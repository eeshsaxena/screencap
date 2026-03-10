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


def _click_event(ts: float, pressed: bool = True) -> Event:
    return Event(timestamp=ts, type="action", data={
        "name": "click", "pressed": pressed, "button": "left",
        "mouse_x": 100, "mouse_y": 100,
    })


def _move_event(ts: float) -> Event:
    return Event(timestamp=ts, type="action", data={
        "name": "move", "mouse_x": 100, "mouse_y": 100,
    })


def _scroll_event(ts: float) -> Event:
    return Event(timestamp=ts, type="action", data={
        "name": "scroll", "mouse_x": 100, "mouse_y": 100, "dx": 0, "dy": -3,
    })


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
    orig_action_aware = config.SCREENSHOT_ACTION_AWARE

    object.__setattr__(config, "SCREENSHOT_DEDUP", True)
    object.__setattr__(config, "SCREENSHOT_MIN_INTERVAL", 1.0)
    object.__setattr__(config, "SCREENSHOT_HASH_THRESHOLD", 8)
    object.__setattr__(config, "RECORD_VIDEO", False)
    object.__setattr__(config, "RECORD_WINDOW_DATA", True)
    object.__setattr__(config, "RECORD_FULL_VIDEO", False)
    object.__setattr__(config, "RECORD_READ_ACTIVE_ELEMENT_STATE", False)
    object.__setattr__(config, "SCREENSHOT_ACTION_AWARE", False)
    yield
    object.__setattr__(config, "SCREENSHOT_DEDUP", orig_dedup)
    object.__setattr__(config, "SCREENSHOT_MIN_INTERVAL", orig_interval)
    object.__setattr__(config, "SCREENSHOT_HASH_THRESHOLD", orig_threshold)
    object.__setattr__(config, "RECORD_VIDEO", orig_video)
    object.__setattr__(config, "RECORD_WINDOW_DATA", orig_window)
    object.__setattr__(config, "RECORD_FULL_VIDEO", orig_full_video)
    object.__setattr__(config, "RECORD_READ_ACTIVE_ELEMENT_STATE", orig_ax)
    object.__setattr__(config, "SCREENSHOT_ACTION_AWARE", orig_action_aware)


def _recording():
    """Create a mock recording object."""
    rec = mock.MagicMock()
    rec.timestamp = time.time()
    return rec


def _run_process_events(events: list[Event], screen_filter=None):
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
    video_wq = mock.MagicMock(spec=SynchronizedQueue)
    perf_q = mock.MagicMock(spec=SynchronizedQueue)

    recording = _recording()
    terminate = multiprocessing.Event()
    started = threading.Event()
    num_screen = multiprocessing.Value("i", 0)
    num_action = multiprocessing.Value("i", 0)
    num_window = multiprocessing.Value("i", 0)
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
            event_q, screen_wq, action_wq, window_wq, video_wq,
            perf_q, recording, terminate, started,
            num_screen, num_action, num_window, num_video,
            screen_filter=screen_filter,
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


def _press_event(ts: float) -> Event:
    """Key press action event (name='press', matching retention filter)."""
    return Event(timestamp=ts, type="action", data={"name": "press", "key": "a"})


class _AdvancingClock:
    """Fake monotonic clock that advances by a fixed step on each call.

    Decoupled from the number of internal time.monotonic() calls in
    process_events() — any refactoring that adds/removes calls just shifts
    the absolute time, it doesn't break the relative intervals.
    """

    def __init__(self, start: float = 0.0, step: float = 0.05):
        self._value = start
        self._step = step

    def __call__(self) -> float:
        v = self._value
        self._value += self._step
        return v


class TestActionAwareSavesDragFrames:
    """ACTION_AWARE=True saves drag frames that dedup would reject."""

    def test_action_aware_saves_drag_frames(self):
        object.__setattr__(config, "SCREENSHOT_ACTION_AWARE", True)
        object.__setattr__(config, "SCREENSHOT_DEDUP", False)
        img = _img()
        t0 = 1000.0

        # start=10.0 clears the initial _last_save_mono=0.0 on first event.
        # step=0.05s → the retention filter's 0.1s drag interval elapses
        # roughly every 2 monotonic() calls, regardless of internal call count.
        clock = _AdvancingClock(start=10.0, step=0.05)

        events = [
            _screen_event(t0, img),
            _window_event(t0 + 0.001),
            _click_event(t0 + 0.01, pressed=True),
            _screen_event(t0 + 0.15, img),
            _move_event(t0 + 0.16),
            _screen_event(t0 + 0.3, img),
            _move_event(t0 + 0.31),
        ]

        with mock.patch("sc_engine.recorder.time") as mock_time:
            mock_time.monotonic = clock
            mock_time.time = time.time
            screen_count, action_count, _, drops = _run_process_events(events)

        # Click always saves; drag moves save when cadence elapses
        assert screen_count >= 2
        assert drops.get("screen_click_save", 0) >= 1


class TestActionAwareTypingUsesTimeFloor:
    """ACTION_AWARE=True throttles typing to ~1fps."""

    def test_typing_throttled(self):
        object.__setattr__(config, "SCREENSHOT_ACTION_AWARE", True)
        object.__setattr__(config, "SCREENSHOT_DEDUP", False)
        img = _img()
        t0 = 1000.0

        # start=10.0 clears the initial _last_save_mono=0.0 on first event.
        # step=0.05s → consecutive press events are ~0.05-0.15s apart in
        # monotonic time, well within the 1.0s type interval.
        clock = _AdvancingClock(start=10.0, step=0.05)

        events = [
            _screen_event(t0, img),
            _window_event(t0 + 0.001),
            _press_event(t0 + 0.01),
            _screen_event(t0 + 0.3, img),
            _press_event(t0 + 0.31),
            _screen_event(t0 + 0.6, img),
            _press_event(t0 + 0.61),
        ]

        with mock.patch("sc_engine.recorder.time") as mock_time:
            mock_time.monotonic = clock
            mock_time.time = time.time
            screen_count, action_count, _, drops = _run_process_events(events)

        # Only first frame saved (typing cadence = 1.0s, clock advances ~0.05s/call)
        assert screen_count == 1
        assert drops.get("screen_cadence_skip", 0) >= 1


class TestActionAwareWithDedupFallthrough:
    """ACTION_AWARE + DEDUP=True: BASELINE decision falls through to dHash."""

    def test_unknown_action_falls_through_to_dedup(self):
        object.__setattr__(config, "SCREENSHOT_ACTION_AWARE", True)
        object.__setattr__(config, "SCREENSHOT_DEDUP", True)
        object.__setattr__(config, "SCREENSHOT_MIN_INTERVAL", 1.0)
        object.__setattr__(config, "SCREENSHOT_HASH_THRESHOLD", 8)
        img = _img()
        t0 = 1000.0

        clock = _AdvancingClock(start=10.0, step=0.05)

        # key.down is unknown to retention filter → BASELINE → dHash gate
        events = [
            _screen_event(t0, img),
            _window_event(t0 + 0.001),
            _action_event(t0 + 0.01),        # first frame → saves (no prior hash)
            _screen_event(t0 + 0.5, img),    # same image, < 1s
            _action_event(t0 + 0.51),        # BASELINE → dHash → time floor skip
        ]

        with mock.patch("sc_engine.recorder.time") as mock_time:
            mock_time.monotonic = clock
            mock_time.time = time.time
            screen_count, _, _, drops = _run_process_events(events)

        # dHash time floor should kick in — only first frame saved
        assert screen_count == 1
        assert drops.get("screen_time_floor", 0) == 1


class _SwitchingScreenFilter:
    """Screen filter that allows first N calls, then blocks.

    Simulates: user scrolls in an allowed app, then switches to a blocked app
    before the settle frame fires.
    """

    cloud_intent = False

    def __init__(self, allow_count: int):
        self._allow_count = allow_count
        self._call_count = 0

    def is_screen_allowed(self, timestamp=None):
        self._call_count += 1
        return self._call_count <= self._allow_count

    def on_window_event(self, data):
        pass

    def on_action_event(self, data):
        pass

    def fail_closed(self):
        pass

    def null_keystroke_content(self, data):
        pass


class TestSettleFrameSavesAfterScrollSilence:
    """Settle frame saves the last screen after scroll silence."""

    def test_settle_saves_screen(self):
        object.__setattr__(config, "SCREENSHOT_ACTION_AWARE", True)
        object.__setattr__(config, "SCREENSHOT_DEDUP", False)
        object.__setattr__(config, "RECORD_VIDEO", False)
        img = _img()
        t0 = 1000.0

        # Large step so settle deadline elapses during queue.Empty
        clock = _AdvancingClock(start=10.0, step=0.5)

        events = [
            _screen_event(t0, img),
            _window_event(t0 + 0.001),
            _scroll_event(t0 + 0.01),
        ]

        with mock.patch("sc_engine.recorder.time") as mock_time:
            mock_time.monotonic = clock
            mock_time.time = time.time
            screen_count, _, _, drops = _run_process_events(events)

        assert drops.get("screen_settle_save", 0) >= 1

    def test_settle_also_saves_video_frame(self):
        object.__setattr__(config, "SCREENSHOT_ACTION_AWARE", True)
        object.__setattr__(config, "SCREENSHOT_DEDUP", False)
        object.__setattr__(config, "RECORD_VIDEO", True)
        object.__setattr__(config, "RECORD_FULL_VIDEO", False)
        img = _img()
        t0 = 1000.0

        clock = _AdvancingClock(start=10.0, step=0.5)

        events = [
            _screen_event(t0, img),
            _window_event(t0 + 0.001),
            _scroll_event(t0 + 0.01),
        ]

        written_types = []

        def capture_process_event(event, wq, wfn, rec, pq, tp):
            written_types.append(event.type)
            return True

        event_q = queue.Queue()
        for ev in events:
            event_q.put(ev)

        recording = _recording()
        terminate = multiprocessing.Event()
        started = threading.Event()
        nums = {k: multiprocessing.Value("i", 0) for k in
                ["screen", "action", "window", "video"]}

        def set_terminate():
            time.sleep(0.1)
            terminate.set()
        t = threading.Thread(target=set_terminate)
        t.start()

        with mock.patch("sc_engine.recorder.time") as mock_time, \
             mock.patch("sc_engine.recorder.process_event", side_effect=capture_process_event):
            mock_time.monotonic = clock
            mock_time.time = time.time
            from sc_engine import recorder as rec_mod
            old_drops = dict(rec_mod._drop_counts)
            rec_mod._drop_counts = {}

            process_events(
                event_q,
                mock.MagicMock(spec=SynchronizedQueue),
                mock.MagicMock(spec=SynchronizedQueue),
                mock.MagicMock(spec=SynchronizedQueue),
                mock.MagicMock(spec=SynchronizedQueue),
                mock.MagicMock(spec=SynchronizedQueue),
                recording, terminate, started,
                nums["screen"], nums["action"], nums["window"],
                nums["video"],
            )
            drops = dict(rec_mod._drop_counts)
            rec_mod._drop_counts = old_drops

        t.join()

        assert drops.get("screen_settle_save", 0) >= 1
        # Settle should write both screen and screen/video
        assert "screen" in written_types
        assert "screen/video" in written_types


class TestSettleFrameBlockedByPrivacy:
    """Settle frame must NOT save when privacy filter blocks at settle time."""

    def test_settle_blocked_after_app_switch(self):
        object.__setattr__(config, "SCREENSHOT_ACTION_AWARE", True)
        object.__setattr__(config, "SCREENSHOT_DEDUP", False)
        object.__setattr__(config, "RECORD_VIDEO", False)
        img = _img()
        t0 = 1000.0

        clock = _AdvancingClock(start=10.0, step=0.5)

        events = [
            _screen_event(t0, img),
            _window_event(t0 + 0.001),
            _scroll_event(t0 + 0.01),
            _scroll_event(t0 + 0.02),
        ]

        # Allow screens during scroll processing, block at settle time
        sf = _SwitchingScreenFilter(allow_count=2)

        with mock.patch("sc_engine.recorder.time") as mock_time:
            mock_time.monotonic = clock
            mock_time.time = time.time
            screen_count, _, _, drops = _run_process_events(
                events, screen_filter=sf,
            )

        assert drops.get("privacy_settle_blocked", 0) >= 1
        assert drops.get("screen_settle_save", 0) == 0
