"""Script for creating Recordings.

Multi-process recording system.
adaptation for per-capture databases.

Usage:

    $ python -m sc_engine.recorder "<description of task>"

"""

import io
import json
import multiprocessing
import os
import queue
import signal
import sys
import threading
import time
import tracemalloc
from collections import defaultdict, namedtuple
from functools import partial
from typing import Any, Callable, Optional

import av
import fire
import numpy as np
import psutil
from loguru import logger
from pympler import tracker
from pynput import keyboard, mouse
from tqdm import tqdm

from sc_engine import utils, video, window
from sc_engine.window.ax_browser_url import (
    ALL_KNOWN_BROWSER_BUNDLES as _BROWSER_BUNDLES,
    extract_browser_url,
    invalidate_url_cache,
)
from sc_engine.ax_cache import AXQueryCache
from sc_engine.config import RecordingConfig, config
from sc_engine.dedup import dhash, hamming_distance
from sc_engine.retention import RetentionDecision, ScreenRetentionFilter
from sc_engine.db import create_db, crud, get_session_for_path
from sc_engine.db.models import ActionEvent, Recording
from sc_engine.extensions import synchronized_queue as sq

try:
    import soundfile
except ImportError:
    soundfile = None

def _send_profiling_via_wormhole(profile_path: str) -> None:
    """Auto-send profiling JSON via Magic Wormhole after recording."""
    import shutil
    import subprocess as _sp

    wormhole_bin = shutil.which("wormhole")
    if not wormhole_bin and not getattr(sys, 'frozen', False):
        # Check Python Scripts dir (Windows, non-frozen only)
        from pathlib import Path

        scripts_dir = Path(sys.executable).parent / "Scripts"
        for candidate in [scripts_dir / "wormhole.exe", scripts_dir / "wormhole"]:
            if candidate.exists():
                wormhole_bin = str(candidate)
                break
    if not wormhole_bin:
        print("wormhole not found. To enable auto-send:")
        print("  pip install magic-wormhole")
        print(f"Profiling saved to: {profile_path}")
        return

    print("Sending profiling via wormhole (waiting for receiver)...")
    print("Give the wormhole code below to the receiver.\n")
    try:
        _sp.run([wormhole_bin, "send", profile_path], check=True)
    except _sp.CalledProcessError:
        print(f"Wormhole send failed. File at: {profile_path}")
    except KeyboardInterrupt:
        print(f"\nCancelled. File at: {profile_path}")


Event = namedtuple("Event", ("timestamp", "type", "data", "extra"), defaults=(None,))

# Placeholder frame interval for cloud-intent blocked intervals (1 FPS)
_PLACEHOLDER_INTERVAL = 1.0


def _make_placeholder_frame(width: int, height: int):
    """Create a near-black placeholder frame with centered 'Recording paused' text.

    Used by cloud-intent recordings to fill blocked-app intervals in the
    video stream. Matches masking.py color constants: (30,30,30) background,
    (180,180,180) label text.
    """
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (width, height), (30, 30, 30))
    draw = ImageDraw.Draw(img)
    label = "Recording paused"
    bbox = draw.textbbox((0, 0), label)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    text_x = (width - text_w) // 2
    text_y = (height - text_h) // 2
    draw.text((text_x, text_y), label, fill=(180, 180, 180))
    return img


EVENT_TYPES = ("screen", "action", "window")
LOG_LEVEL = os.environ.get("SC_LOG_LEVEL", "INFO")

# Configure loguru to use LOG_LEVEL (default stderr handler is DEBUG)
logger.remove()
logger.add(sys.stderr, level=LOG_LEVEL)
# whether to write events of each type in a separate process
PROC_WRITE_BY_EVENT_TYPE = {
    "screen": True,
    "screen/video": True,
    "action": True,
    "window": True,
}
NUM_MEMORY_STATS_TO_LOG = 3

stop_sequence_detected = False

# Screen dimensions are determined per-process in video_pre_callback()
# (mss.grab() is slow from background threads on macOS, so we avoid
# calling take_screenshot() at module level).


def collect_stats(performance_snapshots: list[tracemalloc.Snapshot]) -> None:
    """Collects and appends performance snapshots using tracemalloc.

    Args:
        performance_snapshots (list[tracemalloc.Snapshot]): The list of snapshots.
    """
    performance_snapshots.append(tracemalloc.take_snapshot())


def log_memory_usage(
    tracker: tracker.SummaryTracker,
    performance_snapshots: list[tracemalloc.Snapshot],
) -> None:
    """Logs memory usage stats and allocation trace based on snapshots.

    Args:
        tracker (tracker.SummaryTracker): The tracker to use.
        performance_snapshots (list[tracemalloc.Snapshot]): The list of snapshots.
    """
    assert len(performance_snapshots) == 2, performance_snapshots
    first_snapshot, last_snapshot = performance_snapshots
    stats = last_snapshot.compare_to(first_snapshot, "lineno")

    for stat in stats[:NUM_MEMORY_STATS_TO_LOG]:
        new_KiB = stat.size_diff / 1024
        total_KiB = stat.size / 1024
        new_blocks = stat.count_diff
        total_blocks = stat.count
        source = stat.traceback.format()[0].strip()
        logger.info(f"{source=}")
        logger.info(f"\t{new_KiB=} {total_KiB=} {new_blocks=} {total_blocks=}")

    trace_str = "\n".join(list(tracker.format_diff()))
    logger.info(f"trace_str=\n{trace_str}")


def process_event(
    event: ActionEvent,
    write_q: sq.SynchronizedQueue,
    write_fn: Callable,
    recording: Recording,
    perf_q: sq.SynchronizedQueue,
    terminate_processing: Optional[multiprocessing.Event] = None,
) -> bool:
    """Process an event and take appropriate action based on its type.

    Args:
        event: The event to process.
        write_q: The queue for writing the event.
        write_fn: The function for writing the event.
        recording: The recording object.
        perf_q: The queue for collecting performance statistics.
        terminate_processing: If set, use shorter timeout for prompt shutdown.

    Returns:
        True if the event was successfully queued/written, False if dropped.
    """
    if PROC_WRITE_BY_EVENT_TYPE[event.type]:
        timeout = 0.5 if (terminate_processing and terminate_processing.is_set()) else 1.0
        try:
            write_q.put(event, timeout=timeout)
            return True
        except queue.Full:
            logger.warning(f"write queue full, dropping {event.type} event")
            return False
    else:
        write_fn(recording, event, perf_q)
        return True


@utils.trace(logger)
def process_events(
    event_q: queue.Queue,
    screen_write_q: sq.SynchronizedQueue,
    action_write_q: sq.SynchronizedQueue,
    window_write_q: sq.SynchronizedQueue,
    video_write_q: sq.SynchronizedQueue,
    perf_q: sq.SynchronizedQueue,
    recording: Recording,
    terminate_processing: multiprocessing.Event,
    started_event: threading.Event,
    num_screen_events: multiprocessing.Value,
    num_action_events: multiprocessing.Value,
    num_window_events: multiprocessing.Value,
    num_video_events: multiprocessing.Value,
    screen_filter: Any | None = None,
    dead_queues: set[str] | None = None,
) -> None:
    """Process events from the event queue and write them to write queues.

    Args:
        event_q: A queue with events to be processed.
        screen_write_q: A queue for writing screen events.
        action_write_q: A queue for writing action events.
        window_write_q: A queue for writing window events.
        video_write_q: A queue for writing video events.
        perf_q: A queue for collecting performance data.
        recording: The recording object.
        terminate_processing: An event to signal the termination of the process.
        started_event: Event to set once started.
        num_screen_events: A counter for the number of screen events.
        num_action_events: A counter for the number of action events.
        num_window_events: A counter for the number of window events.
        num_video_events: A counter for the number of video events.
    """
    utils.set_start_time(recording.timestamp)

    logger.info("Starting")

    # Rate-limit accessibility queries to avoid starving target app main thread.
    _AX_QUERY_INTERVAL = config.AX_QUERY_INTERVAL
    _AX_MOVE_QUERY_INTERVAL = config.AX_MOVE_QUERY_INTERVAL
    _last_ax_query_time = 0.0
    _last_ax_move_query_time = 0.0

    # Async AX cache — runs element state queries off the event-processing thread.
    ax_cache: AXQueryCache | None = None
    if config.RECORD_READ_ACTIVE_ELEMENT_STATE:
        ax_cache = AXQueryCache(query_fn=window.get_active_element_state)
        ax_cache.start()

    prev_event = None
    prev_screen_event = None
    prev_window_event = None
    prev_saved_screen_timestamp = 0
    prev_saved_window_timestamp = 0
    prev_saved_screen_hash: int | None = None   # dHash of last saved screenshot
    prev_saved_window_key: tuple[str | None, int | None] = (None, None)  # (title, window_id)
    _drops = defaultdict(int)  # drop counters by event type / group
    started = False
    drain_start = None

    # Dead-queue bypass: map write queues to their writer process names.
    # When a writer dies, record()'s health check adds its name to
    # dead_queues (shared set).  We skip put() for dead writers to avoid
    # 1.0s timeout cascades that stall the entire event pipeline.
    if dead_queues is None:
        dead_queues = set()
    _q_to_writer = {
        id(screen_write_q): "screen_event_writer",
        id(action_write_q): "action_event_writer",
        id(window_write_q): "window_event_writer",
        id(video_write_q): "video_writer",
    }

    def _is_queue_dead(wq: sq.SynchronizedQueue) -> bool:
        writer = _q_to_writer.get(id(wq))
        return writer is not None and writer in dead_queues

    # Variable-rate retention filter (action-aware capture)
    retention_filter = None
    if config.SCREENSHOT_ACTION_AWARE:
        retention_filter = ScreenRetentionFilter(
            drag_interval=config.SCREENSHOT_DRAG_INTERVAL,
            scroll_interval=config.SCREENSHOT_SCROLL_INTERVAL,
            type_interval=config.SCREENSHOT_TYPE_INTERVAL,
            idle_interval=config.SCREENSHOT_IDLE_INTERVAL,
            settle_secs=config.SCREENSHOT_SCROLL_SETTLE,
        )

    # Placeholder frame state (cloud-intent only: fill blocked intervals)
    _cloud_placeholder = getattr(screen_filter, 'cloud_intent', False)
    _placeholder_frame = None
    _last_placeholder_ts = 0.0
    _screen_dims = None
    _in_blocked_interval = False

    def _start_blocked_interval_if_needed(ts):
        """Record the start of a blocked interval (decoupled from placeholder rate)."""
        nonlocal _in_blocked_interval
        if not _in_blocked_interval:
            _in_blocked_interval = True
            try:
                screen_filter.record_block_start(ts)
            except Exception:
                _drops["privacy_filter_error"] += 1

    def _push_placeholder_if_due(ts, screen_data=None):
        """Push placeholder at 1 FPS during blocked cloud-intent intervals."""
        nonlocal _placeholder_frame, _last_placeholder_ts, _screen_dims
        _start_blocked_interval_if_needed(ts)
        now_m = time.monotonic()
        if now_m - _last_placeholder_ts < _PLACEHOLDER_INTERVAL:
            return
        if _screen_dims is None:
            _screen_dims = (
                screen_data.size if screen_data is not None and hasattr(screen_data, 'size')
                else utils.get_monitor_dims()
            )
        if _placeholder_frame is None:
            _placeholder_frame = _make_placeholder_frame(*_screen_dims)
        ph = Event(timestamp=ts, type="screen/video", data=_placeholder_frame)
        if _is_queue_dead(video_write_q):
            _drops["dead_queue_placeholder"] += 1
        elif process_event(ph, video_write_q, write_video_event, recording, perf_q, terminate_processing):
            num_video_events.value += 1
        _last_placeholder_ts = now_m

    def _end_blocked_interval_if_active(ts):
        nonlocal _in_blocked_interval
        if _cloud_placeholder and _in_blocked_interval:
            _in_blocked_interval = False
            try:
                screen_filter.record_block_end(ts)
            except Exception:
                _drops["privacy_filter_error"] += 1
    while not terminate_processing.is_set() or not event_q.empty():
        # Enforce drain deadline once shutdown begins
        if terminate_processing.is_set():
            if drain_start is None:
                drain_start = time.monotonic()
            elif time.monotonic() - drain_start > 10.0:
                logger.warning("Drain deadline exceeded, exiting event_processor")
                break
        # Adaptive timeout: shorter when a settle deadline is pending
        _eq_timeout = 1.0
        if retention_filter is not None and retention_filter.has_pending_settle():
            _eq_timeout = max(0.05, retention_filter.time_until_settle(time.monotonic()))
        try:
            event = event_q.get(timeout=_eq_timeout)
        except queue.Empty:
            # Check settle deadline — force-save current screen as settle frame
            if (retention_filter is not None
                    and prev_screen_event is not None
                    and retention_filter.check_settle(time.monotonic())):
                # Privacy filter: suppress settle frame for blocked apps
                _settle_allowed = (
                    screen_filter.is_screen_allowed(prev_screen_event.timestamp)
                    if screen_filter is not None else True
                )
                if not _settle_allowed:
                    _drops["privacy_settle_blocked"] += 1
                else:
                    _drops["screen_settle_save"] += 1
                    # Re-use existing fan-out: save screen + action-gated video
                    settle_events = [
                        (prev_screen_event, screen_write_q, write_screen_event),
                    ]
                    if config.RECORD_VIDEO and not config.RECORD_FULL_VIDEO:
                        settle_vid = prev_screen_event._replace(type="screen/video")
                        settle_events.append(
                            (settle_vid, video_write_q, write_video_event),
                        )
                    for sev, swq, swfn in settle_events:
                        if _is_queue_dead(swq):
                            _drops["dead_queue_settle"] += 1
                            continue
                        if process_event(sev, swq, swfn, recording, perf_q, terminate_processing):
                            if sev.type == "screen":
                                num_screen_events.value += 1
                                prev_saved_screen_timestamp = prev_screen_event.timestamp
                                if config.SCREENSHOT_DEDUP:
                                    prev_saved_screen_hash = dhash(prev_screen_event.data)
                            elif sev.type == "screen/video":
                                num_video_events.value += 1
            # Push placeholder frames during idle blocked intervals (cloud-intent)
            if (_cloud_placeholder and config.RECORD_VIDEO
                    and not screen_filter.is_screen_allowed()):
                _push_placeholder_if_due(utils.get_timestamp())
            continue
        if not started:
            started_event.set()
            started = True
        logger.trace(f"{event=}")
        assert event.type in EVENT_TYPES, event
        if prev_event is not None:
            try:
                assert event.timestamp > prev_event.timestamp, (
                    event,
                    prev_event,
                )
            except AssertionError:
                delta = event.timestamp - prev_event.timestamp
                log_prev_event = prev_event._replace(data="")
                log_event = event._replace(data="")
                logger.error(f"{delta=} {log_prev_event=} {log_event=}")
                # behavior undefined, swallow for now
                # XXX TODO: mitigate
        if event.type == "screen":
            prev_screen_event = event
            if config.RECORD_FULL_VIDEO:
                # Privacy filter: skip full-video frames for blocked apps
                if screen_filter is not None and not screen_filter.is_screen_allowed(event.timestamp):
                    _drops["privacy_full_video"] += 1
                    # Cloud-intent: push placeholder at 1 FPS instead of gap
                    if _cloud_placeholder:
                        _push_placeholder_if_due(event.timestamp, event.data)
                else:
                    _end_blocked_interval_if_active(event.timestamp)
                    video_event = event._replace(type="screen/video")
                    if _is_queue_dead(video_write_q):
                        _drops["dead_queue_video"] += 1
                    elif process_event(
                        video_event,
                        video_write_q,
                        write_video_event,
                        recording,
                        perf_q,
                        terminate_processing,
                    ):
                        num_video_events.value += 1
                    else:
                        _drops["full_video"] += 1
            elif _cloud_placeholder and config.RECORD_VIDEO:
                # Action-gated mode: push placeholder on screen events during blocked intervals
                if not screen_filter.is_screen_allowed(event.timestamp):
                    _push_placeholder_if_due(event.timestamp, event.data)
                else:
                    _end_blocked_interval_if_active(event.timestamp)
        elif event.type == "window":
            prev_window_event = event
            # Notify privacy filter of window change
            if screen_filter is not None:
                try:
                    screen_filter.on_window_event(event.data)
                except Exception:
                    _drops["privacy_filter_error"] += 1
                    screen_filter.fail_closed()
        elif event.type == "action":
            if prev_screen_event is None:
                logger.warning("Discarding action that came before screen")
                continue
            else:
                event.data["screenshot_timestamp"] = prev_screen_event.timestamp

            if prev_window_event is None:
                if config.RECORD_WINDOW_DATA:
                    logger.warning("Discarding action that came before window")
                    continue
                # Window capture disabled — skip window timestamp requirement
            else:
                event.data["window_event_timestamp"] = prev_window_event.timestamp

            # Enrich with accessibility element state via async cache.
            # Event-aware routing: different event types get different AX
            # query depths and rate limits. Queries run in background thread
            # so the event pipeline is never blocked by slow AX IPC.
            ax_request_id = None
            if ax_cache is not None:
                x = event.data.get("mouse_x")
                y = event.data.get("mouse_y")
                action_name = event.data.get("name", "")
                now = time.monotonic()

                # Key events: skip AX query entirely (no coordinates)
                if action_name in ("key.down", "key.up"):
                    pass
                elif x is not None and y is not None:
                    ax_depth = None
                    should_query = False

                    if action_name in ("click", "singleclick", "doubleclick"):
                        # Clicks: deep query, bypass rate limit
                        ax_depth = config.AX_CLICK_MAX_DEPTH
                        should_query = True
                    elif action_name == "scroll":
                        # Scrolls: medium depth, normal rate limit
                        ax_depth = config.AX_SCROLL_MAX_DEPTH
                        if now - _last_ax_query_time >= _AX_QUERY_INTERVAL:
                            should_query = True
                    elif action_name == "move":
                        # Moves: shallow depth, longer interval
                        ax_depth = config.AX_MOVE_MAX_DEPTH
                        if now - _last_ax_move_query_time >= _AX_MOVE_QUERY_INTERVAL:
                            should_query = True
                            _last_ax_move_query_time = now
                    else:
                        # Unknown action with coordinates: default behavior
                        ax_depth = config.AX_MAX_DEPTH
                        if now - _last_ax_query_time >= _AX_QUERY_INTERVAL:
                            should_query = True

                    if should_query:
                        _last_ax_query_time = now
                        ax_request_id = ax_cache.submit(
                            x, y, max_depth=ax_depth, event_name=action_name
                        )

            # Retrieve async AX result before DB write.
            if ax_request_id is not None:
                # Allow slightly more time for click events (they're high-value).
                ax_timeout = 0.05 if "click" in event.data.get("name", "") else 0.01
                element_state = ax_cache.get(ax_request_id, timeout=ax_timeout)
                if element_state is not None:
                    event.data["element_state"] = element_state

            # Notify privacy filter of AXSecureTextField (Layer 1)
            if screen_filter is not None and event.data.get("element_state"):
                try:
                    screen_filter.on_action_event(event.data)
                except Exception:
                    _drops["privacy_filter_error"] += 1
                    screen_filter.fail_closed()

            # Screenshot dedup gate
            should_save_screen = prev_saved_screen_timestamp < prev_screen_event.timestamp
            current_hash: int | None = None

            # Cache privacy check for this action event (avoids redundant lock + FFI)
            _screen_allowed = screen_filter.is_screen_allowed(prev_screen_event.timestamp) if screen_filter is not None else True

            # Privacy filter: suppress screenshot for blocked apps
            if should_save_screen and screen_filter is not None:
                if not _screen_allowed:
                    should_save_screen = False
                    _drops["privacy_screen"] += 1
                    # Cloud-intent: push placeholder at 1 FPS
                    if _cloud_placeholder and config.RECORD_VIDEO:
                        _push_placeholder_if_due(prev_screen_event.timestamp, prev_screen_event.data)
                elif _cloud_placeholder:
                    _end_blocked_interval_if_active(prev_screen_event.timestamp)

            # Action-type gating (variable-rate capture)
            _retention_skip_dedup = False
            if should_save_screen and retention_filter is not None:
                _r_mono = time.monotonic()
                _r_decision = retention_filter.should_save(
                    event.data.get("name", ""), event.data, _r_mono,
                )
                if _r_decision is RetentionDecision.SAVE:
                    _drops["screen_click_save"] += 1
                    _retention_skip_dedup = True
                elif _r_decision is RetentionDecision.BYPASS_DEDUP:
                    _drops["screen_cadence_bypass"] += 1
                    _retention_skip_dedup = True
                elif _r_decision is RetentionDecision.SKIP:
                    should_save_screen = False
                    _drops["screen_cadence_skip"] += 1
                # BASELINE → fall through to dHash

            if should_save_screen and config.SCREENSHOT_DEDUP and not _retention_skip_dedup:
                if prev_saved_screen_hash is None:
                    pass  # first frame always saved
                else:
                    current_window_key = (
                        (prev_window_event.data.get("title"), prev_window_event.data.get("window_id"))
                        if prev_window_event else (None, None)
                    )
                    window_changed = current_window_key != prev_saved_window_key

                    if not window_changed:
                        elapsed = prev_screen_event.timestamp - prev_saved_screen_timestamp
                        if elapsed < config.SCREENSHOT_MIN_INTERVAL:
                            should_save_screen = False
                            _drops["screen_time_floor"] += 1
                        else:
                            current_hash = dhash(prev_screen_event.data)
                            dist = hamming_distance(current_hash, prev_saved_screen_hash)
                            if dist <= config.SCREENSHOT_HASH_THRESHOLD:
                                should_save_screen = False
                                _drops["screen_dedup"] += 1

            if not should_save_screen:
                event.data["screenshot_timestamp"] = prev_saved_screen_timestamp

            # Drop-coherent fan-out: collect all events for this action as a
            # group. If any put fails, drop the entire group to prevent broken
            # cross-references (e.g., action row pointing to nonexistent screenshot).
            # Action is written LAST so that if a dependency (screen, video,
            # window) fails, the action "anchor" record is never committed.
            events_to_write = []
            if should_save_screen:
                events_to_write.append(
                    (prev_screen_event, screen_write_q, write_screen_event)
                )
                if config.RECORD_VIDEO and not config.RECORD_FULL_VIDEO:
                    video_event = prev_screen_event._replace(type="screen/video")
                    events_to_write.append(
                        (video_event, video_write_q, write_video_event)
                    )
            if prev_window_event is not None:
                if prev_saved_window_timestamp < prev_window_event.timestamp:
                    events_to_write.append(
                        (prev_window_event, window_write_q, write_window_event)
                    )
            # Privacy filter: null sensitive content for blocked apps/inputs.
            # Applies to ALL action events (key, mouse, etc.) — if the screen
            # is blocked, no content fields should survive to disk.
            if screen_filter is not None and not _screen_allowed:
                screen_filter.null_keystroke_content(event.data)
                _drops["privacy_keystroke"] += 1

            # Action event last — the anchor record that references the others
            events_to_write.append((event, action_write_q, write_action_event))

            # Try to write all; if any fails (or dead queue), drop the entire group
            all_ok = True
            _dead_queue_drop = False
            for ev, wq, wfn in events_to_write:
                if _is_queue_dead(wq):
                    all_ok = False
                    _dead_queue_drop = True
                    break
                if not process_event(
                    ev, wq, wfn, recording, perf_q, terminate_processing
                ):
                    all_ok = False
                    break

            if all_ok:
                num_action_events.value += 1
                if any(ev.type == "screen" for ev, _, _ in events_to_write):
                    num_screen_events.value += 1
                    prev_saved_screen_timestamp = prev_screen_event.timestamp
                    prev_saved_screen_hash = current_hash if current_hash is not None else dhash(prev_screen_event.data)
                    prev_saved_window_key = (
                        (prev_window_event.data.get("title"), prev_window_event.data.get("window_id"))
                        if prev_window_event else (None, None)
                    )
                if any(
                    ev.type == "screen/video" for ev, _, _ in events_to_write
                ):
                    num_video_events.value += 1
                if any(ev.type == "window" for ev, _, _ in events_to_write):
                    num_window_events.value += 1
                    prev_saved_window_timestamp = prev_window_event.timestamp
            elif _dead_queue_drop:
                _drops["dead_queue_action_group"] += 1
            else:
                _drops["action_group"] += 1
                logger.warning(
                    "Dropping action event group due to full write queue"
                )
        else:
            raise Exception(f"unhandled {event.type=}")
        del prev_event
        prev_event = event

    # Close any open blocked interval at recording end
    _end_blocked_interval_if_active(utils.get_timestamp())

    # Shut down async AX cache.
    if ax_cache is not None:
        ax_cache.stop()

    # Export drop counts to module-level dict for record() to persist
    if any(_drops.values()):
        logger.warning(f"Events dropped during recording: {dict(_drops)}")
        for k, v in _drops.items():
            _drop_counts[k] = _drop_counts.get(k, 0) + v

    logger.info("Done")


def write_action_event(
    db: crud.SaSession,
    recording: Recording,
    event: Event,
    perf_q: sq.SynchronizedQueue,
) -> None:
    """Write an action event to the database and update the performance queue.

    Args:
        db: The database session.
        recording: The recording object.
        event: An action event to be written.
        perf_q: A queue for collecting performance data.
    """
    assert event.type == "action", event
    crud.insert_action_event(db, recording, event.timestamp, event.data)
    # disabled to increase perf
    # perf_q.put((event.type, event.timestamp, utils.get_timestamp()))


def write_screen_event(
    db: crud.SaSession,
    recording: Recording,
    event: Event,
    perf_q: sq.SynchronizedQueue,
    screenshots_dir: str | None = None,
    **kwargs,
) -> dict[str, Any]:
    """Write a screen event to the database and update the performance queue.

    Args:
        db: The database session.
        recording: The recording object.
        event: A screen event to be written.
        perf_q: A queue for collecting performance data.
        screenshots_dir: Directory for screenshot JPEG files.

    Returns:
        dict containing state for the next iteration.
    """
    assert event.type == "screen", event
    image = event.data
    event_data: dict[str, Any] = {}
    if config.RECORD_IMAGES:
        if screenshots_dir:
            import math

            ts = event.timestamp
            if math.isfinite(ts) and ts > 0:
                filename = f"{ts:.6f}.jpg"
                file_path = os.path.join(screenshots_dir, filename)
                try:
                    image.save(file_path, format="JPEG", quality=95)
                    os.chmod(file_path, 0o600)
                    event_data["image_path"] = f"screenshots/{filename}"
                except OSError:
                    logger.warning(
                        f"Failed to save screenshot to {file_path}, skipping"
                    )
        else:
            with io.BytesIO() as output:
                image.save(output, format="JPEG", quality=95)
                png_data = output.getvalue()
            event_data["png_data"] = png_data
    crud.insert_screenshot(db, recording, event.timestamp, event_data)

    # Persist window geometry alongside the screenshot (if captured)
    window_geometries = event.extra
    if window_geometries is not None:
        try:
            crud.insert_window_geometry(
                db, recording, event.timestamp, json.dumps(window_geometries),
            )
        except Exception:
            logger.debug("Failed to insert window geometry", exc_info=True)

    # disabled to increase perf
    # perf_q.put((event.type, event.timestamp, utils.get_timestamp()))
    return {**kwargs, "screenshots_dir": screenshots_dir}


def write_window_event(
    db: crud.SaSession,
    recording: Recording,
    event: Event,
    perf_q: sq.SynchronizedQueue,
) -> None:
    """Write a window event to the database and update the performance queue.

    Args:
        db: The database session.
        recording: The recording object.
        event: A window event to be written.
        perf_q: A queue for collecting performance data.
    """
    assert event.type == "window", event
    crud.insert_window_event(db, recording, event.timestamp, event.data)
    # disabled to increase perf
    # perf_q.put((event.type, event.timestamp, utils.get_timestamp()))


@utils.trace(logger)
def write_events(
    event_type: str,
    write_fn: Callable,
    write_q: sq.SynchronizedQueue,
    num_events: multiprocessing.Value,
    perf_q: sq.SynchronizedQueue,
    recording: Recording,
    db_path: str,
    terminate_processing: multiprocessing.Event,
    started_event: multiprocessing.Event,
    pre_callback: Callable | None = None,
    post_callback: Callable[[dict], None] | None = None,
    config_overrides: dict[str, object] | None = None,
    flush_requested=None,
    flush_ack_counter=None,
) -> None:
    """Write events of a specific type to the db using the provided write function.

    Args:
        event_type: The type of events to be written.
        write_fn: A function to write events to the database.
        write_q: A queue with events to be written.
        num_events: A counter for the number of events.
        perf_q: A queue for collecting performance data.
        recording: The recording object.
        db_path: Path to the per-capture database file.
        terminate_processing: An event to signal the termination of the process.
        started_event: Event to increment once started.
        pre_callback: Optional function to call before main loop. Takes recording
            timestamp as only argument, returns a state dict.
        post_callback: Optional function to call after main loop. Takes state dict as
            only argument, returns None.
        config_overrides: Optional dict of config overrides to apply in child process.
            Spawn mode on macOS re-imports modules, losing in-memory config changes.
    """
    from sc_engine.config import apply_config_overrides
    apply_config_overrides(config_overrides)

    utils.set_start_time(recording.timestamp)

    logger.info(f"{event_type=} starting")
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    # Batch DB commits for throughput at high FPS (default is 1 = every event).
    crud.BATCH_SIZE = 50
    session = get_session_for_path(db_path)

    if pre_callback:
        state = pre_callback(session, recording)
    else:
        state = None

    num_processed = 0
    progress = None
    started = False
    try:
        while not terminate_processing.is_set() or not write_q.empty():
            if terminate_processing.is_set() and progress is None:
                # if processing is over, create a progress bar
                total_events = num_events.value
                progress = tqdm(
                    total=total_events,
                    desc=f"Writing {event_type} events...",
                    unit="event",
                    colour="green",
                    dynamic_ncols=True,
                )
                # update the progress bar with the number of events that have already
                # been processed
                for _ in range(num_processed):
                    progress.update()
            if not started:
                started_event.set()
                started = True
            try:
                event = write_q.get_nowait()
            except queue.Empty:
                # Check for mid-recording flush request (chunked mode)
                if flush_requested is not None and flush_requested.is_set():
                    crud.flush_buffers(session)
                    with flush_ack_counter.get_lock():
                        flush_ack_counter.value += 1
                time.sleep(0.01)
                continue
            assert event.type == event_type, (event_type, event)
            state = write_fn(session, recording, event, perf_q, **(state or {}))
            num_processed += 1
            with num_events.get_lock():
                if progress is not None:
                    if progress.total < num_events.value:
                        # update the total number of events in the progress bar
                        progress.total = num_events.value
                        progress.refresh()
                    progress.update()
            logger.debug(f"{event_type=} written")
    finally:
        # Flush any partial batch left in crud insert buffers.
        crud.flush_buffers(session)

        if post_callback:
            post_callback(state)

        if progress is not None:
            progress.close()

    logger.info(f"{event_type=} done")


def video_pre_callback(
    db: crud.SaSession, recording: Recording, video_dir: str = None,
) -> dict[str, Any]:
    """Function to call before main loop.

    Args:
        db: The database session.
        recording: The recording object.
        video_dir: Directory for video files.

    Returns:
        dict[str, Any]: The updated state.
    """
    video_file_path = video.get_video_file_path(recording.timestamp, video_dir)
    # Get actual screen dimensions from an initial screenshot.
    init_screenshot = utils.take_screenshot()
    if init_screenshot is not None:
        screen_width, screen_height = init_screenshot.size
    else:
        logger.warning("take_screenshot() returned None in video_pre_callback, using get_monitor_dims()")
        screen_width, screen_height = utils.get_monitor_dims()
    video_container, video_stream, video_start_timestamp = (
        video.initialize_video_writer(video_file_path, screen_width, screen_height)
    )
    crud.update_video_start_time(db, recording, video_start_timestamp)
    return {
        "video_container": video_container,
        "video_stream": video_stream,
        "video_start_timestamp": video_start_timestamp,
        "last_pts": 0,
        "video_file_path": video_file_path,
    }


def video_post_callback(state: dict) -> None:
    """Function to call after main loop.

    Args:
        state (dict): The current state.
    """
    if state is None or "last_frame" not in state:
        logger.warning("No video frames captured — skipping finalization")
        if state and "video_container" in state:
            state["video_container"].close()
        return
    video.finalize_video_writer(
        state["video_container"],
        state["video_stream"],
        state["video_start_timestamp"],
        state["last_frame"],
        state["last_frame_timestamp"],
        state["last_pts"],
        state["video_file_path"],
    )


def chunked_video_pre_callback(
    db: crud.SaSession, recording: Recording, video_dir: str = None,
    chunk_duration: float = 3600.0, chunk_rotate_q=None,
) -> dict[str, Any]:
    """Pre-callback for chunked video mode using ChunkedVideoWriter."""
    init_screenshot = utils.take_screenshot()
    if init_screenshot is not None:
        screen_width, screen_height = init_screenshot.size
    else:
        logger.warning("take_screenshot() returned None in chunked_video_pre_callback")
        screen_width, screen_height = utils.get_monitor_dims()

    writer = video.ChunkedVideoWriter(
        output_dir=video_dir or os.getcwd(),
        width=screen_width,
        height=screen_height,
        chunk_duration=chunk_duration,
        chunk_rotate_q=chunk_rotate_q,
    )

    video_start_timestamp = utils.get_timestamp()
    crud.update_video_start_time(db, recording, video_start_timestamp)
    return {
        "chunked_writer": writer,
        "video_start_timestamp": video_start_timestamp,
    }


def chunked_write_video_event(
    db: crud.SaSession,
    recording_timestamp: float,
    event: "Event",
    perf_q: sq.SynchronizedQueue,
    chunked_writer: "video.ChunkedVideoWriter" = None,
    video_start_timestamp: float = 0,
    **kwargs: dict,
) -> dict[str, Any]:
    """Write a screen event using ChunkedVideoWriter."""
    assert event.type == "screen/video"
    screenshot_image = event.data
    screenshot_timestamp = event.timestamp
    force_key_frame = not hasattr(chunked_writer, '_has_written')
    if force_key_frame:
        chunked_writer._has_written = True
    chunked_writer.write_frame(screenshot_image, screenshot_timestamp, force_key_frame)
    return {
        **kwargs,
        "chunked_writer": chunked_writer,
        "video_start_timestamp": video_start_timestamp,
        "last_frame": screenshot_image,
        "last_frame_timestamp": screenshot_timestamp,
    }


def chunked_video_post_callback(state: dict) -> None:
    """Post-callback for chunked video mode."""
    if state is None:
        return
    writer = state.get("chunked_writer")
    if writer is not None:
        writer.close()


def screen_pre_callback(
    db: crud.SaSession,
    recording: Recording,
    screenshots_dir: str | None = None,
) -> dict[str, Any]:
    """Create screenshots directory before the screen writer loop starts.

    Args:
        db: The database session (unused, required by callback signature).
        recording: The recording object (unused, required by callback signature).
        screenshots_dir: Path to the screenshots directory.

    Returns:
        State dict with screenshots_dir for the write loop.
    """
    if screenshots_dir:
        os.makedirs(screenshots_dir, mode=0o700, exist_ok=True)
    return {"screenshots_dir": screenshots_dir}


def write_video_event(
    db: crud.SaSession,
    recording_timestamp: float,
    event: Event,
    perf_q: sq.SynchronizedQueue,
    video_container: av.container.OutputContainer,
    video_stream: av.stream.Stream,
    video_start_timestamp: float,
    last_pts: int = 0,
    num_copies: int = 2,
    **kwargs: dict,
) -> dict[str, Any]:
    """Write a screen event to the video file and update the performance queue.

    Args:
        db: The database session.
        recording_timestamp: The timestamp of the recording.
        event: A screen event to be written.
        perf_q: A queue for collecting performance data.
        video_container (av.container.OutputContainer): The output container to which
            the frame is written.
        video_stream (av.stream.Stream): The video stream within the container.
        video_start_timestamp (float): The base timestamp from which the video
            recording started.
        last_pts: The last presentation timestamp.
        num_copies: The number of times to write the frame.

    Returns:
        dict containing state.
    """
    assert event.type == "screen/video"
    screenshot_image = event.data
    screenshot_timestamp = event.timestamp
    force_key_frame = last_pts == 0
    # ensure that the first frame is available (otherwise occasionally it is not)
    # TODO: why isn't force_key_frame sufficient?
    if last_pts != 0:
        num_copies = 1
    for _ in range(num_copies):
        last_pts = video.write_video_frame(
            video_container,
            video_stream,
            screenshot_image,
            screenshot_timestamp,
            video_start_timestamp,
            last_pts,
            force_key_frame,
        )
    # disabled to increase perf
    # perf_q.put((event.type, event.timestamp, utils.get_timestamp()))
    return {
        **kwargs,
        **{
            "video_container": video_container,
            "video_stream": video_stream,
            "video_start_timestamp": video_start_timestamp,
            "last_frame": screenshot_image,
            "last_frame_timestamp": screenshot_timestamp,
            "last_pts": last_pts,
        },
    }


def trigger_action_event(
    event_q: queue.Queue, action_event_args: dict[str, Any]
) -> None:
    """Triggers an action event and adds it to the event queue.

    Queues the event immediately without blocking on accessibility queries.
    The element_state is populated later in process_events() to avoid
    blocking the pynput callback thread.

    Backpressure strategy:
    - Mouse moves: put_nowait() — fully redundant at 100+/sec, silent drop.
    - Clicks/keys/scrolls: put(timeout=0.05) — pynput thread can tolerate
      50ms block (not a CGEventTap), drop with warning on Full.

    Args:
        event_q: The event queue to add the action event to.
        action_event_args: A dictionary containing the arguments for the action event.

    Returns:
        None
    """
    event = Event(utils.get_timestamp(), "action", action_event_args)
    action_name = action_event_args.get("name", "")

    if action_name == "move":
        try:
            event_q.put_nowait(event)
        except queue.Full:
            pass  # silent drop — next move arrives in ~10ms
    else:
        try:
            event_q.put(event, timeout=0.05)
        except queue.Full:
            _drop_counts["action"] = _drop_counts.get("action", 0) + 1
            # Only log action_name (event type), never action_event_args
            # (contains keystrokes, coordinates, and other user input data)
            logger.warning(f"event_q full, dropping {action_name} action event")


def on_move(event_q: queue.Queue, x: int, y: int, injected: bool = False) -> None:
    """Handles the 'move' event.

    Args:
        event_q: The event queue to add the 'move' event to.
        x: The x-coordinate of the mouse.
        y: The y-coordinate of the mouse.
        injected: Whether the event was injected or not.

    Returns:
        None
    """
    logger.debug(f"{x=} {y=} {injected=}")
    if not injected:
        data = {"name": "move", "mouse_x": x, "mouse_y": y}
        if _current_pressure > 0.0:
            data["mouse_pressure"] = _current_pressure
        if _current_modifier_flags:
            data["modifier_flags"] = _current_modifier_flags
        trigger_action_event(event_q, data)


def on_click(
    event_q: queue.Queue,
    x: int,
    y: int,
    button: mouse.Button,
    pressed: bool,
    injected: bool = False,
) -> None:
    """Handles the 'click' event.

    Args:
        event_q: The event queue to add the 'click' event to.
        x: The x-coordinate of the mouse.
        y: The y-coordinate of the mouse.
        button: The mouse button.
        pressed: Whether the button is pressed or released.
        injected: Whether the event was injected or not.

    Returns:
        None
    """
    logger.debug(f"{x=} {y=} {button=} {pressed=} {injected=}")
    if not injected:
        data = {
            "name": "click",
            "mouse_x": x,
            "mouse_y": y,
            "mouse_button_name": button.name,
            "mouse_pressed": pressed,
        }
        if _current_pressure > 0.0:
            data["mouse_pressure"] = _current_pressure
        if _current_modifier_flags:
            data["modifier_flags"] = _current_modifier_flags
        trigger_action_event(event_q, data)


def on_scroll(
    event_q: queue.Queue,
    x: int,
    y: int,
    dx: int,
    dy: int,
    injected: bool = False,
) -> None:
    """Handles the 'scroll' event.

    Args:
        event_q: The event queue to add the 'scroll' event to.
        x: The x-coordinate of the mouse.
        y: The y-coordinate of the mouse.
        dx: The horizontal scroll amount.
        dy: The vertical scroll amount.
        injected: Whether the event was injected or not.

    Returns:
        None
    """
    logger.debug(f"{x=} {y=} {dx=} {dy=} {injected=}")
    if not injected:
        data = {
            "name": "scroll",
            "mouse_x": x,
            "mouse_y": y,
            "mouse_dx": dx,
            "mouse_dy": dy,
        }
        if _current_modifier_flags:
            data["modifier_flags"] = _current_modifier_flags
        if _current_scroll_phase:
            data["scroll_phase"] = _current_scroll_phase
        if _current_momentum_phase:
            data["momentum_phase"] = _current_momentum_phase
        if _current_is_continuous:
            data["is_continuous"] = _current_is_continuous
        trigger_action_event(event_q, data)


def handle_key(
    event_q: queue.Queue,
    event_name: str,
    key: keyboard.KeyCode,
    canonical_key: keyboard.KeyCode,
) -> None:
    """Handles a key event.

    Args:
        event_q: The event queue to add the key event to.
        event_name: The name of the key event.
        key: The key code of the key event.
        canonical_key: The canonical key code of the key event.

    Returns:
        None
    """
    attr_names = [
        "name",
        "char",
        "vk",
    ]
    attrs = {
        f"key_{attr_name}": getattr(key, attr_name, None) for attr_name in attr_names
    }
    logger.debug(f"{attrs=}")
    canonical_attrs = {
        f"canonical_key_{attr_name}": getattr(canonical_key, attr_name, None)
        for attr_name in attr_names
    }
    logger.debug(f"{canonical_attrs=}")
    trigger_action_event(event_q, {"name": event_name, **attrs, **canonical_attrs})


def read_screen_events(
    event_q: queue.Queue,
    terminate_processing: multiprocessing.Event,
    recording: Recording,
    started_event: threading.Event,
    _screen_timing: list | None = None,
) -> None:
    """Read screen events and add them to the event queue.

    Captures at most ``config.SCREEN_CAPTURE_FPS`` frames per second.
    Set to 0 for unlimited (legacy behaviour).

    Args:
        event_q: A queue for adding screen events.
        terminate_processing: An event to signal the termination of the process.
        recording: The recording object.
        started_event: Event to set once started.
        _screen_timing: If provided, append (screenshot_dur, total_dur) per iteration.
    """
    utils.set_start_time(recording.timestamp)

    fps = config.SCREEN_CAPTURE_FPS
    min_interval = 1.0 / fps if fps > 0 else 0.0

    # Window geometry capture — import here to avoid module-level side effects
    # in spawned child processes.
    _get_geometries = None
    try:
        from sc_engine.window._macos import get_all_window_geometries
        _get_geometries = get_all_window_geometries
    except (ImportError, OSError):
        pass

    logger.info(f"Starting (fps={fps}, min_interval={min_interval:.3f}s)")
    started = False
    _geom_slow_count = 0
    while not terminate_processing.is_set():
        t_start = time.perf_counter()
        screenshot = utils.take_screenshot()
        t_screenshot = time.perf_counter()
        if screenshot is None:
            logger.warning("Screenshot was None")
            continue

        # Capture window geometry immediately after screenshot for accurate bounds
        window_geometries = None
        if _get_geometries is not None:
            t_geom_start = time.perf_counter()
            try:
                window_geometries = _get_geometries()
            except Exception:
                pass
            t_geom = time.perf_counter() - t_geom_start
            if t_geom > 0.01:
                _geom_slow_count += 1
                if _geom_slow_count <= 5:
                    logger.warning(
                        f"get_all_window_geometries took {t_geom*1000:.1f}ms "
                        f"(>10ms threshold)"
                    )

        if not started:
            started_event.set()
            started = True
        # Yield to let action/window events through when queue is near-full
        if event_q.qsize() > int(event_q.maxsize * 0.75):
            time.sleep(0.1)
        try:
            event_q.put(
                Event(utils.get_timestamp(), "screen", screenshot, window_geometries),
                timeout=0.5,
            )
        except queue.Full:
            _drop_counts["screen"] = _drop_counts.get("screen", 0) + 1
            logger.debug("event_q full, dropping screen frame")
            continue
        # Throttle: sleep for the remainder of the frame interval
        if min_interval > 0:
            elapsed = time.perf_counter() - t_start
            sleep_time = min_interval - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)
        if _screen_timing is not None:
            t_end = time.perf_counter()
            _screen_timing.append((t_screenshot - t_start, t_end - t_start))
    logger.info("Done")


@utils.trace(logger)
def read_window_events(
    event_q: queue.Queue,
    terminate_processing: multiprocessing.Event,
    recording: Recording,
    started_event: threading.Event,
) -> None:
    """Read window events and add them to the event queue.

    Args:
        event_q: A queue for adding window events.
        terminate_processing: An event to signal the termination of the process.
        recording: The recording object.
        started_event: Event to set once started.
    """
    utils.set_start_time(recording.timestamp)

    poll_interval = max(config.AX_QUERY_INTERVAL, 0.1)
    logger.info(f"Starting (poll_interval={poll_interval:.1f}s)")
    prev_window_data = {}
    started = False
    while not terminate_processing.is_set():
        window_data = window.get_active_window_data()
        if not window_data:
            time.sleep(poll_interval)
            continue

        if not started:
            started_event.set()
            started = True

        # Enrich browser windows with URL from address bar
        _bundle = window_data.get("app_bundle_id") or ""
        if _bundle in _BROWSER_BUNDLES:
            _pid = (
                (window_data.get("state") or {})
                .get("meta", {})
                .get("kCGWindowOwnerPID")
            )
            _wid = str(window_data.get("window_id") or "")
            _title = window_data.get("title") or ""
            if _pid is not None:
                # Invalidate cache on window change so we re-query the URL.
                # Only invalidate on window_id change — title-only changes
                # (e.g. Gmail unread count) don't change the URL.
                if _wid != str(prev_window_data.get("window_id") or ""):
                    invalidate_url_cache(_pid, _wid)
                window_data["browser_url"] = extract_browser_url(
                    _pid, _bundle, _wid, _title,
                )
                if window_data.get("browser_url"):
                    from urllib.parse import urlparse
                    _domain = urlparse(window_data["browser_url"]).hostname
                    logger.debug(f"browser_url domain={_domain!r}")

        if window_data["title"] != prev_window_data.get("title") or window_data[
            "window_id"
        ] != prev_window_data.get("window_id"):
            # TODO: fix exception sometimes triggered by the next line on win32:
            #   File "\Python39\lib\threading.py" line 917, in run
            #   File "sc_engine/recorder.py" in read window events
            #   File "...\env\lib\site-packages\loguru\logger.py" line 1977, in info
            #   File "...\env\lib\site-packages\loguru\_logger.py", line 1964, in _log
            #       for handler in core.handlers.values):
            #   RuntimeError: dictionary changed size during iteration
            _window_data = window_data
            _window_data.pop("state")
            _window_data.pop("browser_url", None)
            logger.info(f"{_window_data=}")
        if window_data != prev_window_data:
            logger.debug("Queuing window event for writing")
            try:
                event_q.put(
                    Event(
                        utils.get_timestamp(),
                        "window",
                        window_data,
                    ),
                    timeout=0.5,
                )
                prev_window_data = window_data
            except queue.Full:
                _drop_counts["window"] = _drop_counts.get("window", 0) + 1
                logger.warning("event_q full, dropping window event")
                # prev_window_data stays unchanged so we retry next iteration
        time.sleep(poll_interval)


@utils.trace(logger)
def performance_stats_writer(
    perf_q: sq.SynchronizedQueue,
    recording: Recording,
    db_path: str,
    terminate_processing: multiprocessing.Event,
    started_event: multiprocessing.Event,
) -> None:
    """Write performance stats to the database.

    Each entry includes the event type, start time, and end time.

    Args:
        perf_q: A queue for collecting performance data.
        recording: The recording object.
        db_path: Path to the per-capture database file.
        terminate_processing: An event to signal the termination of the process.
        started_event: Event to set once started.
    """
    utils.set_start_time(recording.timestamp)

    logger.info("Performance stats writer starting")
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    started = False
    session = get_session_for_path(db_path)
    while not terminate_processing.is_set() or not perf_q.empty():
        if not started:
            started_event.set()
            started = True
        try:
            event_type, start_time, end_time = perf_q.get_nowait()
        except queue.Empty:
            time.sleep(0.01)
            continue

        crud.insert_perf_stat(
            session,
            recording,
            event_type,
            start_time,
            end_time,
        )
    logger.info("Performance stats writer done")


def memory_writer(
    recording: Recording,
    db_path: str,
    terminate_processing: multiprocessing.Event,
    record_pid: int,
    started_event: multiprocessing.Event,
) -> None:
    """Writes memory usage statistics to the database.

    Args:
        recording (Recording): The recording object.
        db_path: Path to the per-capture database file.
        terminate_processing (multiprocessing.Event): The event used to terminate
          the process.
        record_pid (int): The process ID to monitor memory usage for.
        started_event: Event to set once started.

    Returns:
        None
    """
    utils.set_start_time(recording.timestamp)

    logger.info("Memory writer starting")
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    process = psutil.Process(record_pid)

    started = False
    session = get_session_for_path(db_path)
    while not terminate_processing.is_set():
        if not started:
            started_event.set()
            started = True
        memory_usage_bytes = 0

        try:
            memory_info = process.memory_info()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            break
        rss = memory_info.rss  # Resident Set Size: non-swapped physical memory
        memory_usage_bytes += rss

        for child in process.children(recursive=True):
            # after ctrl+c, children may terminate before the next line
            try:
                child_memory_info = child.memory_info()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            child_rss = child_memory_info.rss
            rss += child_rss

        timestamp = utils.get_timestamp()

        crud.insert_memory_stat(
            session,
            recording,
            rss,
            timestamp,
        )
        time.sleep(1)  # sample once per second instead of tight loop
    logger.info("Memory writer done")


def _get_display_layout() -> list[dict] | None:
    """Snapshot all connected display bounds and pixel ratios.

    Returns a list of dicts with keys: display_id, x, y, w, h, pixel_ratio.
    All coordinates are in logical macOS points (global display coordinate space).
    Returns None on non-macOS or on failure.
    """
    if sys.platform != "darwin":
        return None
    try:
        import Quartz

        err, display_ids, count = Quartz.CGGetActiveDisplayList(16, None, None)
        if err != 0 or not display_ids:
            return None
        displays = []
        for did in display_ids[:count]:
            bounds = Quartz.CGDisplayBounds(did)
            mode = Quartz.CGDisplayCopyDisplayMode(did)
            if mode:
                phys_w = Quartz.CGDisplayModeGetPixelWidth(mode)
                log_w = Quartz.CGDisplayModeGetWidth(mode)
                ratio = phys_w / log_w if log_w > 0 else 1.0
            else:
                ratio = 1.0
            displays.append({
                "display_id": int(did),
                "x": bounds.origin.x,
                "y": bounds.origin.y,
                "w": bounds.size.width,
                "h": bounds.size.height,
                "pixel_ratio": ratio,
            })
        return displays if displays else None
    except Exception:
        return None


@utils.trace(logger)
def create_recording(
    task_description: str,
    capture_dir: str,
) -> tuple[Recording, str]:
    """Create a new recording entry in the per-capture database.

    Args:
        task_description: A text description of the task being recorded.
        capture_dir: Path to the capture directory.

    Returns:
        tuple of (Recording object, db_path).
    """
    os.makedirs(capture_dir, exist_ok=True)
    db_path = os.path.join(capture_dir, "recording.db")

    from sc_engine.platform import get_display_pixel_ratio

    timestamp = utils.set_start_time()
    monitor_width, monitor_height = utils.get_monitor_dims()
    double_click_distance_pixels = utils.get_double_click_distance_pixels()
    double_click_interval_seconds = utils.get_double_click_interval_seconds()
    pixel_ratio = get_display_pixel_ratio()

    config = {}
    display_layout = _get_display_layout()
    if display_layout:
        config["displays"] = display_layout

    screenshot = utils.take_screenshot()
    if screenshot is not None:
        config["screenshot_size"] = list(screenshot.size)

    logger.info(
        f"Display layout: displays={display_layout}, "
        f"screenshot_size={screenshot.size if screenshot else None}, "
        f"primary_pixel_ratio={pixel_ratio}, "
        f"monitor_dims=({monitor_width}, {monitor_height})"
    )

    recording_data = {
        # TODO: rename
        "timestamp": timestamp,
        "monitor_width": monitor_width,
        "monitor_height": monitor_height,
        "pixel_ratio": pixel_ratio,
        "double_click_distance_pixels": double_click_distance_pixels,
        "double_click_interval_seconds": double_click_interval_seconds,
        "platform": sys.platform,
        "task_description": task_description,
        "config": config if config else None,
    }
    engine, Session = create_db(db_path)
    session = Session()
    recording = crud.insert_recording(session, recording_data)
    logger.info(f"{recording=}")
    return recording, db_path


def read_keyboard_events(
    event_q: queue.Queue,
    terminate_processing: multiprocessing.Event,
    recording: Recording,
    started_event: threading.Event,
) -> None:
    """Reads keyboard events and adds them to the event queue.

    Args:
        event_q (queue.Queue): The event queue to add the keyboard events to.
        terminate_processing (multiprocessing.Event): The event to signal termination
          of event reading.
        recording (Recording): The recording object.
        started_event: Event to set once started.

    Returns:
        None
    """
    # create list of indices for sequence detection
    # one index for each stop sequence in config.STOP_SEQUENCES
    stop_sequences = config.STOP_SEQUENCES
    stop_sequence_indices = [0 for _ in stop_sequences]

    def on_press(
        event_q: queue.Queue,
        key: keyboard.Key | keyboard.KeyCode,
        injected: bool = False,
    ) -> None:
        """Event handler for key press events.

        Args:
            event_q (queue.Queue): The event queue for processing key events.
            key (keyboard.KeyboardEvent): The key event object representing
              the pressed key.
            injected (bool): A flag indicating whether the key event was injected.

        Returns:
            None
        """
        canonical_key = keyboard_listener.canonical(key)
        logger.debug(f"{key=} {injected=} {canonical_key=}")
        if not injected:
            handle_key(event_q, "press", key, canonical_key)

        # stop sequence code
        nonlocal stop_sequence_indices
        global stop_sequence_detected
        canonical_key_name = getattr(canonical_key, "name", None)

        for i in range(0, len(stop_sequences)):
            # check each stop sequence
            stop_sequence = stop_sequences[i]
            # stop_sequence_indices[i] is the index for this stop sequence
            # get canonical KeyCode of current letter in this sequence
            canonical_sequence = keyboard_listener.canonical(
                keyboard.KeyCode.from_char(stop_sequence[stop_sequence_indices[i]])
            )

            # Check if the pressed key matches the current key in this sequence
            if (
                canonical_key == canonical_sequence
                or canonical_key_name == stop_sequence[stop_sequence_indices[i]]
            ):
                # increment this index
                stop_sequence_indices[i] += 1
            else:
                # Reset index since pressed key doesn't match sequence key
                stop_sequence_indices[i] = 0

            # Check if the entire sequence has been entered correctly
            if stop_sequence_indices[i] >= len(stop_sequence):
                stop_sequence_indices[i] = 0
                logger.info("Stop sequence entered! Stopping recording now.")
                stop_sequence_detected = True

    def on_release(
        event_q: queue.Queue,
        key: keyboard.Key | keyboard.KeyCode,
        injected: bool = False,
    ) -> None:
        """Event handler for key release events.

        Args:
            event_q (queue.Queue): The event queue for processing key events.
            key (keyboard.KeyboardEvent): The key event object representing
              the released key.
            injected (bool): A flag indicating whether the key event was injected.

        Returns:
            None
        """
        canonical_key = keyboard_listener.canonical(key)
        logger.debug(f"{key=} {injected=} {canonical_key=}")
        if not injected:
            handle_key(event_q, "release", key, canonical_key)

    utils.set_start_time(recording.timestamp)

    keyboard_listener = keyboard.Listener(
        on_press=partial(on_press, event_q),
        on_release=partial(on_release, event_q),
    )
    keyboard_listener.start()

    # NOTE: listener may not have actually started by now
    # TODO: handle race condition, e.g. by sending synthetic events from main thread
    started_event.set()

    terminate_processing.wait()
    keyboard_listener.stop()


def read_mouse_events(
    event_q: queue.Queue,
    terminate_processing: multiprocessing.Event,
    recording: Recording,
    started_event: threading.Event,
) -> None:
    """Reads mouse events and adds them to the event queue.

    Args:
        event_q: The event queue to add the mouse events to.
        terminate_processing: The event to signal termination of event reading.
        recording: The recording object.
        started_event: Event to set once started.

    Returns:
        None
    """
    utils.set_start_time(recording.timestamp)

    mouse_listener = mouse.Listener(
        on_move=partial(on_move, event_q),
        on_click=partial(on_click, event_q),
        on_scroll=partial(on_scroll, event_q),
    )
    mouse_listener.start()

    # NOTE: listener may not have actually started by now
    # TODO: handle race condition, e.g. by sending synthetic events from main thread
    started_event.set()

    terminate_processing.wait()
    mouse_listener.stop()


# Shared drop counters: written by reader threads, gesture tap, process_events.
# Python's GIL makes dict operations safe enough for counters (minor undercount
# possible on truly concurrent increments, acceptable for observability data).
_drop_counts: dict = {}

# Shared pressure state: written by gesture tap, read by pynput callbacks.
# Python's GIL makes float reads/writes atomic; no lock needed.
_current_pressure: float = 0.0
_current_modifier_flags: int = 0
_current_scroll_phase: int = 0
_current_momentum_phase: int = 0
_current_is_continuous: bool = False


def read_gesture_events(
    event_q: queue.Queue,
    terminate_processing: multiprocessing.Event,
    recording: Recording,
    started_event: threading.Event,
) -> None:
    """Capture trackpad gesture events via a custom CGEventTap (macOS only).

    Captures pinch-to-zoom (magnify), two-finger rotation, and three-finger
    swipe events that pynput's mouse listener does not handle.

    Uses kCGEventTapOptionListenOnly which requires Input Monitoring
    permission (NOT Accessibility).

    Args:
        event_q: The event queue to add gesture events to.
        terminate_processing: Event to signal termination.
        recording: The recording object.
        started_event: Event to set once started.
    """
    import Quartz
    from AppKit import NSEvent
    from CoreFoundation import (
        CFMachPortCreateRunLoopSource,
        CFRunLoopAddSource,
        CFRunLoopAddTimer,
        CFRunLoopGetCurrent,
        CFRunLoopRun,
        CFRunLoopStop,
        CFRunLoopTimerCreate,
        kCFAllocatorDefault,
        kCFRunLoopDefaultMode,
    )

    utils.set_start_time(recording.timestamp)

    # NSEvent type constants
    NS_EVENT_TYPE_MAGNIFY = 30
    NS_EVENT_TYPE_ROTATE = 18
    NS_EVENT_TYPE_SWIPE = 31
    NS_EVENT_TYPE_SMART_MAGNIFY = 32

    # CGEventTap mask: type 29 (NSEventTypeGesture) for trackpad gestures,
    # plus mouse event types for pressure extraction (Force Touch / tablet).
    gesture_mask = (
        Quartz.CGEventMaskBit(29)  |    # NSEventTypeGesture
        Quartz.CGEventMaskBit(1)   |    # kCGEventLeftMouseDown
        Quartz.CGEventMaskBit(2)   |    # kCGEventLeftMouseUp
        Quartz.CGEventMaskBit(3)   |    # kCGEventRightMouseDown
        Quartz.CGEventMaskBit(4)   |    # kCGEventRightMouseUp
        Quartz.CGEventMaskBit(5)   |    # kCGEventMouseMoved
        Quartz.CGEventMaskBit(6)   |    # kCGEventLeftMouseDragged
        Quartz.CGEventMaskBit(7)   |    # kCGEventRightMouseDragged
        Quartz.CGEventMaskBit(8)   |    # kCGEventScrollWheel
        Quartz.CGEventMaskBit(25)  |    # kCGEventOtherMouseDown
        Quartz.CGEventMaskBit(26)  |    # kCGEventOtherMouseUp
        Quartz.CGEventMaskBit(27)       # kCGEventOtherMouseDragged
    )

    run_loop_ref = [None]  # mutable container for the CFRunLoop reference

    # CGEvent types for mouse events used in pressure extraction
    _MOUSE_EVENT_TYPES = {1, 2, 3, 4, 5, 6, 7, 25, 26, 27}
    # Mouse up types — reset pressure after release
    _MOUSE_UP_TYPES = {2, 4, 26}  # kCGEventLeftMouseUp, kCGEventRightMouseUp, kCGEventOtherMouseUp

    def _enqueue_gesture(name, x, y, **extra):
        """Enqueue a gesture event via put_nowait (CGEventTap cannot block)."""
        data = {"name": name, "mouse_x": x, "mouse_y": y}
        data.update(extra)
        try:
            event_q.put_nowait(Event(utils.get_timestamp(), "action", data))
        except queue.Full:
            _drop_counts["gesture"] = _drop_counts.get("gesture", 0) + 1
            logger.warning(f"event_q full, dropping {name} gesture event")

    def gesture_callback(_proxy, event_type, cg_event, _refcon):
        """CGEventTap callback — converts CGEvent to NSEvent and enqueues.

        Handles both gesture events (magnify, rotate, swipe) and mouse events
        (for pressure extraction from Force Touch trackpads and tablets).
        """
        global _current_pressure, _current_modifier_flags, _current_scroll_phase, _current_momentum_phase, _current_is_continuous
        try:
            ns_event = NSEvent.eventWithCGEvent_(cg_event)
            if ns_event is None:
                return cg_event

            ns_type = ns_event.type()

            if ns_type == NS_EVENT_TYPE_MAGNIFY:
                mag = ns_event.magnification()
                if mag == 0.0:
                    return cg_event
                loc = Quartz.CGEventGetLocation(cg_event)
                _enqueue_gesture("magnify", loc.x, loc.y, mouse_dx=mag, mouse_dy=0.0)

            elif ns_type == NS_EVENT_TYPE_ROTATE:
                rot = ns_event.rotation()
                if rot == 0.0:
                    return cg_event
                loc = Quartz.CGEventGetLocation(cg_event)
                _enqueue_gesture("rotate", loc.x, loc.y, mouse_dx=rot, mouse_dy=0.0)

            elif ns_type == NS_EVENT_TYPE_SWIPE:
                dx = ns_event.deltaX()
                dy = ns_event.deltaY()
                if dx == 0.0 and dy == 0.0:
                    return cg_event
                loc = Quartz.CGEventGetLocation(cg_event)
                _enqueue_gesture("scroll", loc.x, loc.y, mouse_dx=dx, mouse_dy=dy)

            elif ns_type == NS_EVENT_TYPE_SMART_MAGNIFY:
                loc = Quartz.CGEventGetLocation(cg_event)
                _enqueue_gesture("smart_magnify", loc.x, loc.y)

            elif event_type == 8:  # kCGEventScrollWheel
                _current_modifier_flags = Quartz.CGEventGetFlags(cg_event)
                _current_scroll_phase = Quartz.CGEventGetIntegerValueField(cg_event, 99)
                _current_momentum_phase = Quartz.CGEventGetIntegerValueField(cg_event, 123)
                _current_is_continuous = bool(Quartz.CGEventGetIntegerValueField(cg_event, 88))

            elif event_type in _MOUSE_EVENT_TYPES:
                # Mouse event — extract pressure and modifier flags for shared state.
                # pynput's callbacks read these globals to include in event data.
                _current_modifier_flags = Quartz.CGEventGetFlags(cg_event)
                if event_type in _MOUSE_UP_TYPES:
                    # Reset immediately on mouse-up so pynput's callback
                    # doesn't read stale pressure from the previous down/drag.
                    _current_pressure = 0.0
                else:
                    pressure = 0.0

                    # Check tablet pressure first (CGEvent field 19 = kCGTabletEventPointPressure)
                    subtype = Quartz.CGEventGetIntegerValueField(cg_event, 7)  # kCGMouseEventSubtype
                    if subtype == 1:  # kCGEventMouseSubtypeTabletPoint
                        pressure = Quartz.CGEventGetDoubleValueField(cg_event, 19)

                    # Fall back to NSEvent.pressure() (Force Touch trackpad)
                    if pressure == 0.0:
                        pressure = ns_event.pressure()

                    _current_pressure = pressure

        except Exception:
            # Never let exceptions escape a C callback
            logger.exception("Error in gesture event callback")

        return cg_event

    # Create the event tap
    tap = Quartz.CGEventTapCreate(
        Quartz.kCGSessionEventTap,
        Quartz.kCGHeadInsertEventTap,
        Quartz.kCGEventTapOptionListenOnly,
        gesture_mask,
        gesture_callback,
        None,
    )

    if tap is None:
        logger.warning(
            "Failed to create gesture CGEventTap. "
            "Ensure Input Monitoring permission is granted in "
            "System Settings > Privacy & Security > Input Monitoring. "
            "Recording will continue without trackpad gesture capture."
        )
        started_event.set()
        return

    # Wire tap into a CFRunLoop
    source = CFMachPortCreateRunLoopSource(kCFAllocatorDefault, tap, 0)
    loop = CFRunLoopGetCurrent()
    run_loop_ref[0] = loop
    CFRunLoopAddSource(loop, source, kCFRunLoopDefaultMode)
    Quartz.CGEventTapEnable(tap, True)

    # Periodic timer to check for termination and re-enable the tap
    # (macOS can silently disable taps when SecureInput is active).
    CHECK_INTERVAL = 0.5  # seconds

    def timer_callback(_timer, _info):
        if terminate_processing.is_set():
            Quartz.CGEventTapEnable(tap, False)
            CFRunLoopStop(loop)
        else:
            # Re-enable tap in case macOS disabled it (SecureInput bug)
            if not Quartz.CGEventTapIsEnabled(tap):
                logger.debug("Re-enabling gesture event tap (was disabled)")
                Quartz.CGEventTapEnable(tap, True)

    timer = CFRunLoopTimerCreate(
        kCFAllocatorDefault,
        Quartz.CFAbsoluteTimeGetCurrent() + CHECK_INTERVAL,
        CHECK_INTERVAL,
        0, 0,
        timer_callback,
        None,
    )
    CFRunLoopAddTimer(loop, timer, kCFRunLoopDefaultMode)

    started_event.set()

    # Block this thread in the CFRunLoop until stopped
    CFRunLoopRun()
    logger.debug("Gesture event reader stopped")


def record_audio(
    recording: Recording,
    db_path: str,
    terminate_processing: multiprocessing.Event,
    started_event: multiprocessing.Event,
    audio_rotate_q=None,
    audio_ack_q=None,
) -> None:
    """Record audio narration during the recording and store data in database.

    Uses a streaming FLAC writer to keep RAM bounded (~2 MB) regardless of
    recording duration. A flush thread periodically drains the audio callback
    buffer and appends PCM frames to a single open SoundFile writer.

    Args:
        recording: The recording object.
        db_path: Path to the per-capture database file.
        terminate_processing: An event to signal the termination of the process.
        started_event: Event to set once started.
    """
    utils.set_start_time(recording.timestamp)

    signal.signal(signal.SIGINT, signal.SIG_IGN)

    import sounddevice

    FLUSH_INTERVAL_SECS = 30
    SAMPLERATE = 16000
    CHANNELS = 1

    # Locked buffer — audio_callback appends, flush thread drains
    audio_buffer: list[np.ndarray] = []
    buffer_lock = threading.Lock()

    def audio_callback(
        indata: np.ndarray, frames: int, time: Any, status: sounddevice.CallbackFlags
    ) -> None:
        """Callback function used when new audio frames are recorded.

        Note: time is of type cffi.FFI.CData, but since we don't use this argument
        and we also don't use the cffi library, the Any type annotation is used.
        """
        with buffer_lock:
            audio_buffer.append(indata.copy())

    def _drain_buffer() -> np.ndarray | None:
        """Swap out the buffer under lock, return concatenated frames or None."""
        with buffer_lock:
            if not audio_buffer:
                return None
            drained = audio_buffer[:]
            audio_buffer.clear()
        return np.concatenate(drained, axis=0)

    # Track current chunk index for chunked audio
    _current_chunk_idx = [0]  # mutable container

    def _rotate_audio(sf_writer_ref, capture_dir, new_idx, *, is_final=False):
        """Close current FLAC, open new one for the next chunk.

        When is_final=True, closes and acks but does NOT open a new file
        or advance the chunk index — there is no next chunk.
        """
        # Drain and write remaining buffer
        frames = _drain_buffer()
        if frames is not None:
            try:
                sf_writer_ref[0].write(frames)
            except Exception as e:
                logger.error(f"Audio rotation drain failed: {e}")
        sf_writer_ref[0].close()

        # Send ack for completed chunk
        if audio_ack_q is not None:
            try:
                audio_ack_q.put({"type": "audio_rotated", "completed_index": _current_chunk_idx[0]}, timeout=5)
            except Exception:
                logger.error("Failed to send audio rotation ack")

        if is_final:
            sf_writer_ref[0] = None  # mark as finalized
            return

        _current_chunk_idx[0] = new_idx
        new_path = capture_dir / f"audio_{new_idx:04d}.flac"
        sf_writer_ref[0] = soundfile.SoundFile(
            str(new_path), mode="w", samplerate=SAMPLERATE,
            channels=CHANNELS, format="FLAC",
        )
        logger.info(f"Audio rotated to {new_path}")

    def _flush_loop(
        sf_writer_ref: list,
        stop_event: threading.Event,
        capture_dir,
    ) -> None:
        """Periodically drain audio_buffer and write frames to the FLAC file."""
        while not stop_event.wait(timeout=FLUSH_INTERVAL_SECS):
            # Check for rotation requests before flushing
            if audio_rotate_q is not None:
                while True:
                    try:
                        msg = audio_rotate_q.get_nowait()
                        if msg.get("type") in ("chunk_rotated", "final_chunk"):
                            new_idx = msg.get("completed_index", 0) + 1
                            _rotate_audio(sf_writer_ref, capture_dir, new_idx,
                                          is_final=msg["type"] == "final_chunk")
                    except Exception:
                        break

            if sf_writer_ref[0] is None:
                return  # finalized by final_chunk rotation

            frames = _drain_buffer()
            if frames is not None:
                try:
                    sf_writer_ref[0].write(frames)
                    logger.debug(f"Flushed {len(frames)} audio frames to disk")
                except Exception as e:
                    logger.error(f"Audio flush failed: {e}")
                    return

    # Open streaming FLAC writer before starting the audio stream
    from pathlib import Path as _Path

    capture_dir = _Path(db_path).parent
    _is_chunked = audio_rotate_q is not None
    if _is_chunked:
        audio_flac_path = capture_dir / "audio_0000.flac"
    else:
        audio_flac_path = capture_dir / "audio.flac"

    sf_writer = soundfile.SoundFile(
        str(audio_flac_path),
        mode="w",
        samplerate=SAMPLERATE,
        channels=CHANNELS,
        format="FLAC",
    )
    sf_writer_ref = [sf_writer]  # mutable container for rotation

    # Start the flush thread
    flush_stop = threading.Event()
    flush_thread = threading.Thread(
        target=_flush_loop,
        args=(sf_writer_ref, flush_stop, capture_dir),
        daemon=False,
    )
    flush_thread.start()

    # Open InputStream and start recording
    audio_stream = sounddevice.InputStream(
        callback=audio_callback, samplerate=SAMPLERATE, channels=CHANNELS
    )
    logger.info("Audio recording started.")
    start_timestamp = utils.get_timestamp()
    audio_stream.start()

    # NOTE: listener may not have actually started by now
    started_event.set()

    terminate_processing.wait()
    audio_stream.stop()
    audio_stream.close()

    # Signal flush thread to stop and wait for it
    flush_stop.set()
    flush_thread.join(timeout=10)

    # Process any remaining rotation events
    if audio_rotate_q is not None:
        while True:
            try:
                msg = audio_rotate_q.get_nowait()
                if msg.get("type") in ("chunk_rotated", "final_chunk"):
                    new_idx = msg.get("completed_index", 0) + 1
                    _rotate_audio(sf_writer_ref, capture_dir, new_idx,
                                  is_final=msg["type"] == "final_chunk")
            except Exception:
                break

    # Final drain — write any remaining buffered frames (skip if already
    # finalized by a final_chunk rotation above).
    if sf_writer_ref[0] is not None:
        final_frames = _drain_buffer()
        if final_frames is not None:
            try:
                sf_writer_ref[0].write(final_frames)
                logger.debug(f"Final flush: {len(final_frames)} audio frames")
            except Exception as e:
                logger.error(f"Final audio flush failed: {e}")

        # Close writer — finalizes FLAC headers
        sf_writer_ref[0].close()

    # Derive current audio path from chunk index (audio_flac_path may be stale
    # if chunks were rotated/deleted during recording)
    if _is_chunked:
        _final_audio_path = capture_dir / f"audio_{_current_chunk_idx[0]:04d}.flac"
    else:
        _final_audio_path = audio_flac_path
    logger.info(f"Audio saved to {_final_audio_path}")

    # Send final audio ack
    if audio_ack_q is not None:
        try:
            audio_ack_q.put({"type": "audio_final", "completed_index": _current_chunk_idx[0]}, timeout=5)
        except Exception:
            logger.error("Failed to send audio_final ack")

    if not _final_audio_path.exists():
        logger.info("Final audio file not on disk (may have been uploaded and deleted)")
        return

    flac_size = _final_audio_path.stat().st_size
    if not flac_size:
        logger.warning("Zero-length audio recording — skipping DB insert")
        return

    logger.info(f"Size of compressed audio data: {flac_size} bytes")

    session = get_session_for_path(db_path)
    crud.insert_audio_info(
        session,
        recording,
        start_timestamp,
        SAMPLERATE,
        [],  # word_list — populated later by _auto_transcribe()
    )


@logger.catch
@utils.trace(logger)
def record(
    task_description: str,
    capture_dir: str = None,
    # these should be Event | None, but this raises:
    #   TypeError: unsupported operand type(s) for |: 'method' and 'NoneType'
    # type(multiprocessing.Event) appears to be <class 'method'>
    # TODO: fix this
    terminate_processing: multiprocessing.Event = None,
    terminate_recording: multiprocessing.Event = None,
    status_pipe: multiprocessing.connection.Connection | None = None,
    log_memory: bool = config.LOG_MEMORY,
    # Optional shared counters — if None, record() creates its own.
    # Pass externally-created Values to read counts from outside (e.g. Recorder).
    num_action_events: multiprocessing.Value = None,
    num_screen_events: multiprocessing.Value = None,
    num_window_events: multiprocessing.Value = None,
    num_video_events: multiprocessing.Value = None,
    send_profile: bool = False,
    recording_config: RecordingConfig | None = None,
    chunk_rotate_q=None,
    flush_requested=None,
    flush_ack_counter=None,
    audio_rotate_q=None,
    audio_ack_q=None,
    screen_filter: Any | None = None,
) -> None:
    """Record Screenshots/ActionEvents/WindowEvents.

    Args:
        task_description: A text description of the task to be recorded.
        terminate_processing: An event to signal the termination of the events
        processing.
        terminate_recording: An event to signal the termination of the recording.
        status_pipe: A connection to communicate recording status.
        log_memory: Whether to log memory usage.
    """
    assert config.RECORD_VIDEO or config.RECORD_IMAGES, (
        config.RECORD_VIDEO,
        config.RECORD_IMAGES,
    )

    # Build config overrides dict to propagate to spawned child processes.
    # On macOS, spawn mode re-imports modules, losing in-memory config changes.
    if recording_config is not None:
        from sc_engine.config import build_config_overrides
        _config_overrides = build_config_overrides(recording_config)
    else:
        _config_overrides = None

    # logically it makes sense to communicate from here, but when running
    # from the tray it takes too long
    # TODO: fix this
    # if status_pipe:
    #    status_pipe.send({"type": "record.starting"})

    _profile_start = time.perf_counter()
    _profile_is_main_thread = threading.current_thread() is threading.main_thread()

    logger.info(f"{task_description=}")

    if capture_dir is None:
        capture_dir = os.path.join(os.getcwd(), "capture")
    recording, db_path = create_recording(task_description, capture_dir)
    recording_timestamp = recording.timestamp

    # Pre-import pyobjc symbols on the main thread — pyobjc's lazy-loading
    # bridge is not thread-safe for first-time resolution.
    if sys.platform == "darwin":
        try:
            import Quartz as _q  # noqa: F401
            from AppKit import NSEvent as _ns  # noqa: F401
            _ = (_q.CGEventTapCreate, _q.CGEventMaskBit, _q.kCGSessionEventTap,
                 _q.kCGHeadInsertEventTap, _q.kCGEventTapOptionListenOnly,
                 _q.CGEventTapEnable, _q.CGEventTapIsEnabled,
                 _q.CGEventGetLocation, _q.CFAbsoluteTimeGetCurrent,
                 _q.CGEventGetFlags, _q.CGEventGetIntegerValueField)
            _ = _ns.eventWithCGEvent_
        except (ImportError, AttributeError):
            pass  # pyobjc not available; gesture capture will be skipped

    _IMAGE_QUEUE_SIZE = 20   # queues carrying PIL Images / video frames
    _META_QUEUE_SIZE = 100   # queues carrying small dicts

    event_q = queue.Queue(maxsize=_IMAGE_QUEUE_SIZE)
    screen_write_q = sq.SynchronizedQueue(maxsize=_IMAGE_QUEUE_SIZE)
    action_write_q = sq.SynchronizedQueue(maxsize=_META_QUEUE_SIZE)
    window_write_q = sq.SynchronizedQueue(maxsize=_META_QUEUE_SIZE)
    video_write_q = sq.SynchronizedQueue(maxsize=_IMAGE_QUEUE_SIZE)
    # perf_q: unbounded — tiny 3-tuples (~120 bytes each), bounded perf_q
    # risks cascade deadlock (all writers block → all write queues fill)
    # Reset module-level drop counters for this recording session
    global _drop_counts
    _drop_counts = {}
    perf_q = sq.SynchronizedQueue()
    if terminate_processing is None:
        terminate_processing = multiprocessing.Event()
    task_by_name = {}
    task_started_events = {}
    _screen_timing = []  # per-iteration (screenshot_dur, total_dur) for profiling

    if config.RECORD_WINDOW_DATA:
        window_event_reader = threading.Thread(
            target=read_window_events,
            args=(
                event_q,
                terminate_processing,
                recording,
                task_started_events.setdefault(
                    "window_event_reader", threading.Event()
                ),
            ),
        )
        window_event_reader.start()
        task_by_name["window_event_reader"] = window_event_reader

    screen_event_reader = threading.Thread(
        target=read_screen_events,
        args=(
            event_q,
            terminate_processing,
            recording,
            task_started_events.setdefault("screen_event_reader", threading.Event()),
            _screen_timing,
        ),
    )
    screen_event_reader.start()
    task_by_name["screen_event_reader"] = screen_event_reader

    keyboard_event_reader = threading.Thread(
        target=read_keyboard_events,
        args=(
            event_q,
            terminate_processing,
            recording,
            task_started_events.setdefault("keyboard_event_reader", threading.Event()),
        ),
    )
    keyboard_event_reader.start()
    task_by_name["keyboard_event_reader"] = keyboard_event_reader

    mouse_event_reader = threading.Thread(
        target=read_mouse_events,
        args=(
            event_q,
            terminate_processing,
            recording,
            task_started_events.setdefault("mouse_event_reader", threading.Event()),
        ),
    )
    mouse_event_reader.start()
    task_by_name["mouse_event_reader"] = mouse_event_reader

    # Start gesture event reader (macOS only — captures trackpad pinch/rotate/swipe)
    if sys.platform == "darwin":
        gesture_event_reader = threading.Thread(
            target=read_gesture_events,
            args=(
                event_q,
                terminate_processing,
                recording,
                task_started_events.setdefault(
                    "gesture_event_reader", threading.Event()
                ),
            ),
            daemon=True,
            name="gesture_event_reader",
        )
        gesture_event_reader.start()
        task_by_name["gesture_event_reader"] = gesture_event_reader

    if num_action_events is None:
        num_action_events = multiprocessing.Value("i", 0)
    if num_screen_events is None:
        num_screen_events = multiprocessing.Value("i", 0)
    if num_window_events is None:
        num_window_events = multiprocessing.Value("i", 0)
    if num_video_events is None:
        num_video_events = multiprocessing.Value("i", 0)

    # Shared between main loop and event_processor thread (same process, GIL-safe)
    dead_queues: set[str] = set()

    event_processor = threading.Thread(
        target=process_events,
        args=(
            event_q,
            screen_write_q,
            action_write_q,
            window_write_q,
            video_write_q,
            perf_q,
            recording,
            terminate_processing,
            task_started_events.setdefault("event_processor", threading.Event()),
            num_screen_events,
            num_action_events,
            num_window_events,
            num_video_events,
            screen_filter,
            dead_queues,
        ),
    )
    event_processor.start()
    task_by_name["event_processor"] = event_processor

    _screenshots_dir = os.path.join(capture_dir, "screenshots")
    screen_event_writer = multiprocessing.Process(
        target=utils.WrapStdout(write_events),
        args=(
            "screen",
            write_screen_event,
            screen_write_q,
            num_screen_events,
            perf_q,
            recording,
            db_path,
            terminate_processing,
            task_started_events.setdefault(
                "screen_event_writer", multiprocessing.Event()
            ),
            partial(screen_pre_callback, screenshots_dir=_screenshots_dir),
        ),
        kwargs={
            "config_overrides": _config_overrides,
            "flush_requested": flush_requested,
            "flush_ack_counter": flush_ack_counter,
        },
    )
    screen_event_writer.start()
    task_by_name["screen_event_writer"] = screen_event_writer

    action_event_writer = multiprocessing.Process(
        target=utils.WrapStdout(write_events),
        args=(
            "action",
            write_action_event,
            action_write_q,
            num_action_events,
            perf_q,
            recording,
            db_path,
            terminate_processing,
            task_started_events.setdefault(
                "action_event_writer", multiprocessing.Event()
            ),
        ),
        kwargs={
            "config_overrides": _config_overrides,
            "flush_requested": flush_requested,
            "flush_ack_counter": flush_ack_counter,
        },
    )
    action_event_writer.start()
    task_by_name["action_event_writer"] = action_event_writer

    if config.RECORD_WINDOW_DATA:
        window_event_writer = multiprocessing.Process(
            target=utils.WrapStdout(write_events),
            args=(
                "window",
                write_window_event,
                window_write_q,
                num_window_events,
                perf_q,
                recording,
                db_path,
                terminate_processing,
                task_started_events.setdefault(
                    "window_event_writer", multiprocessing.Event()
                ),
            ),
            kwargs={
                "config_overrides": _config_overrides,
                "flush_requested": flush_requested,
                "flush_ack_counter": flush_ack_counter,
            },
        )
        window_event_writer.start()
        task_by_name["window_event_writer"] = window_event_writer

    if config.RECORD_VIDEO:
        _use_chunked = config.VIDEO_CHUNK_DURATION > 0
        if _use_chunked:
            _v_pre = partial(
                chunked_video_pre_callback,
                video_dir=capture_dir,
                chunk_duration=config.VIDEO_CHUNK_DURATION,
                chunk_rotate_q=chunk_rotate_q,
            )
            _v_write = chunked_write_video_event
            _v_post = chunked_video_post_callback
        else:
            _v_pre = partial(video_pre_callback, video_dir=capture_dir)
            _v_write = write_video_event
            _v_post = video_post_callback

        video_writer = multiprocessing.Process(
            target=utils.WrapStdout(write_events),
            args=(
                "screen/video",
                _v_write,
                video_write_q,
                num_video_events,
                perf_q,
                recording,
                db_path,
                terminate_processing,
                task_started_events.setdefault("video_writer", multiprocessing.Event()),
                _v_pre,
                _v_post,
            ),
            kwargs={
                "config_overrides": _config_overrides,
                "flush_requested": flush_requested,
                "flush_ack_counter": flush_ack_counter,
            },
        )
        video_writer.start()
        task_by_name["video_writer"] = video_writer

    if config.RECORD_AUDIO:
        audio_recorder = multiprocessing.Process(
            target=utils.WrapStdout(record_audio),
            args=(
                recording,
                db_path,
                terminate_processing,
                task_started_events.setdefault(
                    "audio_event_writer", multiprocessing.Event()
                ),
            ),
            kwargs={
                "audio_rotate_q": audio_rotate_q,
                "audio_ack_q": audio_ack_q,
            },
        )
        audio_recorder.start()
        task_by_name["audio_recorder"] = audio_recorder

    terminate_perf_event = multiprocessing.Event()
    # disabled to increase perf
    # perf_stats_writer = multiprocessing.Process(
    #     target=utils.WrapStdout(performance_stats_writer),
    #     args=(
    #         perf_q,
    #         recording,
    #         db_path,
    #         terminate_perf_event,
    #         task_started_events.setdefault(
    #             "perf_stats_writer", multiprocessing.Event()
    #         ),
    #     ),
    # )
    # perf_stats_writer.start()
    # task_by_name["perf_stats_writer"] = perf_stats_writer

    # disabled to increase perf
    # if config.PLOT_PERFORMANCE:
    #     record_pid = os.getpid()
    #     mem_writer = multiprocessing.Process(
    #         target=utils.WrapStdout(memory_writer),
    #         args=(
    #             recording,
    #             db_path,
    #             terminate_perf_event,
    #             record_pid,
    #             task_started_events.setdefault("mem_writer", multiprocessing.Event()),
    #         ),
    #     )
    #     mem_writer.start()
    #     task_by_name["mem_writer"] = mem_writer

    if log_memory:
        performance_snapshots = []
        _tracker = tracker.SummaryTracker()
        tracemalloc.start()
        collect_stats(performance_snapshots)

    # TODO: discard events until everything is ready

    # Wait for all to signal they've started
    expected_starts = len(task_by_name)
    logger.info(f"{expected_starts=}")
    while True:
        started_tasks = sum(event.is_set() for event in task_started_events.values())
        if started_tasks >= expected_starts:
            break
        waiting_for = [
            task for task, event in task_started_events.items() if not event.is_set()
        ]
        logger.info(f"Waiting for tasks to start: {waiting_for}")
        logger.info(f"Started tasks: {started_tasks}/{expected_starts}")
        time.sleep(1)  # Sleep to reduce busy waiting

    for _ in range(5):
        logger.info("*" * 40)
    logger.info("All readers and writers have started. Waiting for input events...")

    if status_pipe:
        status_pipe.send({"type": "record.started"})

    global stop_sequence_detected
    stop_sequence_detected = False

    _CRITICAL_TASKS = frozenset({
        "action_event_writer", "video_writer",
        "screen_event_writer", "event_processor",
    })

    try:
        while not (stop_sequence_detected or terminate_processing.is_set()):
            # Health check: detect crashed child processes/threads
            for name, task in task_by_name.items():
                if name in dead_queues:
                    continue
                if not task.is_alive():
                    exitcode = getattr(task, "exitcode", None)
                    pid = getattr(task, "pid", None)
                    logger.error(
                        f"Task '{name}' (pid={pid}) died unexpectedly "
                        f"(exitcode={exitcode})"
                    )
                    dead_queues.add(name)
                    if status_pipe:
                        status_pipe.send({
                            "type": "record.child_died",
                            "task_name": name,
                            "exitcode": exitcode,
                            "pid": pid,
                            "is_critical": name in _CRITICAL_TASKS,
                        })
                    if name in _CRITICAL_TASKS:
                        logger.error(
                            f"Critical task '{name}' died — stopping recording"
                        )
                        terminate_processing.set()
                        break
            time.sleep(1)
        terminate_processing.set()
    except KeyboardInterrupt:
        terminate_processing.set()

    if status_pipe:
        status_pipe.send({"type": "record.stopping"})

    if log_memory:
        collect_stats(performance_snapshots)
        log_memory_usage(_tracker, performance_snapshots)

    def join_tasks(task_names: list[str], timeout: float = 10.0) -> None:
        for task_name in task_names:
            if task_name not in task_by_name:
                continue
            task = task_by_name[task_name]
            logger.info(f"joining {task_name=}...")
            task.join(timeout=timeout)
            if task.is_alive():
                logger.warning(f"{task_name} did not exit in {timeout}s, terminating")
                if hasattr(task, "terminate"):
                    task.terminate()
                    task.join(timeout=5)
                if hasattr(task, "is_alive") and task.is_alive():
                    logger.warning(f"{task_name} still alive after terminate, killing")
                    if hasattr(task, "kill"):
                        task.kill()
                        task.join(timeout=2)

    # Reader threads and lightweight writers — 10s is enough
    join_tasks(
        [
            "window_event_reader",
            "screen_event_reader",
            "keyboard_event_reader",
            "mouse_event_reader",
            "event_processor",
            "screen_event_writer",
            "action_event_writer",
            "window_event_writer",
        ]
    )
    # Video finalization can take >10s (ffmpeg fMP4 close) — give it 30s
    join_tasks(["video_writer"], timeout=30.0)
    _vw = task_by_name.get("video_writer")
    if _vw is not None and hasattr(_vw, "exitcode") and _vw.exitcode:
        logger.warning(f"video_writer exited with code {_vw.exitcode}")
    # Audio FLAC close needs extra time too
    join_tasks(["audio_recorder"], timeout=15.0)

    # Clean up SynchronizedQueues to prevent feeder-thread hangs at atexit.
    for q in (screen_write_q, action_write_q, window_write_q,
              browser_write_q, video_write_q, perf_q):
        try:
            q.cancel_join_thread()
            q.close()
        except Exception:
            pass

    terminate_perf_event.set()
    # disabled to increase perf
    # join_tasks(
    #     [
    #         "perf_stats_writer",
    #         "mem_writer",
    #     ]
    # )

    # disabled to increase perf
    # if config.PLOT_PERFORMANCE:
    #     try:
    #         from sc_engine import plotting
    #
    #         session = get_session_for_path(db_path)
    #         plotting.plot_performance(
    #             session, recording, save_dir=capture_dir,
    #         )
    #     except ImportError:
    #         logger.warning("matplotlib not installed, skipping performance plot")

    logger.info(f"Saved {recording_timestamp=}")

    session = get_session_for_path(db_path)
    if not getattr(config, 'SKIP_POST_PROCESS', False):
        crud.post_process_events(session, recording)

    # --- Profiling summary ---
    _profile_duration = time.perf_counter() - _profile_start
    _profile_data = {
        "duration_seconds": round(_profile_duration, 2),
        "main_thread": _profile_is_main_thread,
        "platform": sys.platform,
        "python_version": sys.version,
        "threads_started": list(task_by_name.keys()),
        "thread_count": threading.active_count(),
        "event_counts": {
            "action": num_action_events.value,
            "screen": num_screen_events.value,
            "window": num_window_events.value,
            "video": num_video_events.value,
        },
        "screen_timing": {},
        "config": {
            "RECORD_VIDEO": config.RECORD_VIDEO,
            "RECORD_AUDIO": config.RECORD_AUDIO,
            "RECORD_IMAGES": config.RECORD_IMAGES,
            "RECORD_WINDOW_DATA": config.RECORD_WINDOW_DATA,
            "RECORD_FULL_VIDEO": config.RECORD_FULL_VIDEO,
            "PLOT_PERFORMANCE": config.PLOT_PERFORMANCE,
            "SCREEN_CAPTURE_FPS": config.SCREEN_CAPTURE_FPS,
        },
        "capture_dir": capture_dir,
    }
    # Compute screen timing stats
    if _screen_timing:
        ss_durs = [t[0] for t in _screen_timing]
        total_durs = [t[1] for t in _screen_timing]
        _profile_data["screen_timing"] = {
            "iterations": len(_screen_timing),
            "screenshot_avg_ms": round(sum(ss_durs) / len(ss_durs) * 1000, 1),
            "screenshot_max_ms": round(max(ss_durs) * 1000, 1),
            "screenshot_min_ms": round(min(ss_durs) * 1000, 1),
            "total_avg_ms": round(sum(total_durs) / len(total_durs) * 1000, 1),
            "total_max_ms": round(max(total_durs) * 1000, 1),
        }

    # Screenshot dedup summary
    _dedup_drops = _drop_counts.get("screen_dedup", 0) + _drop_counts.get("screen_time_floor", 0)
    if _dedup_drops > 0:
        _saved = num_screen_events.value
        _total_candidates = _saved + _dedup_drops
        _pct = (_dedup_drops / _total_candidates * 100) if _total_candidates > 0 else 0
        _profile_data["screenshot_dedup"] = {
            "saved": _saved,
            "total_candidates": _total_candidates,
            "reduction_pct": round(_pct, 1),
            "drops_hash": _drop_counts.get("screen_dedup", 0),
            "drops_time_floor": _drop_counts.get("screen_time_floor", 0),
        }

    # Merge drop counts from all threads (readers, processor, gesture tap)
    if _drop_counts:
        _profile_data["drops"] = dict(_drop_counts)

    _profile_path = os.path.join(capture_dir, "profiling.json")
    try:
        import json as _json
        with open(_profile_path, "w") as _f:
            _json.dump(_profile_data, _f, indent=2)
        logger.info(f"Profiling saved to {_profile_path}")

        # Print compact summary
        print("\n=== Recording Profile ===")
        print(f"Duration: {_profile_duration:.1f}s")
        print(f"Main thread: {_profile_is_main_thread}")
        print(f"Threads started: {len(task_by_name)}")
        for k, v in _profile_data["event_counts"].items():
            rate = v / _profile_duration if _profile_duration > 0 else 0
            print(f"  {k}: {v} events ({rate:.1f}/s)")
        if _screen_timing:
            st = _profile_data["screen_timing"]
            print(f"  screenshot: avg={st['screenshot_avg_ms']}ms "
                  f"max={st['screenshot_max_ms']}ms "
                  f"min={st['screenshot_min_ms']}ms")
        if "screenshot_dedup" in _profile_data:
            sd = _profile_data["screenshot_dedup"]
            print(f"Screenshot dedup: saved {sd['saved']} of {sd['total_candidates']} "
                  f"candidates ({sd['reduction_pct']}% reduction)")
        print(f"Config: WINDOW_DATA={config.RECORD_WINDOW_DATA} "
              f"VIDEO={config.RECORD_VIDEO} "
              f"PLOT_PERF={config.PLOT_PERFORMANCE} "
              f"FPS={config.SCREEN_CAPTURE_FPS}")
        print("=========================\n")

        # Auto-send profiling via wormhole if requested
        if send_profile:
            _send_profiling_via_wormhole(_profile_path)
    except Exception as exc:
        logger.warning(f"Profiling save/send failed: {exc}")

    if terminate_recording is not None:
        terminate_recording.set()

    # TODO: consolidate terminate_recording and status_pipe
    if status_pipe:
        status_pipe.send({"type": "record.stopped"})


class Recorder:
    """High-level recording interface.

    Wraps the legacy ``record()`` function with a clean Python API:

    - Constructor parameters override config defaults (``capture_video``, etc.)
    - Runtime introspection (``event_count``, ``is_recording``)
    - Post-recording access to ``CaptureSession``

    Usage::

        with Recorder('./my_capture', task_description='Demo task',
                       capture_video=True, capture_audio=False) as recorder:
            recorder.wait_for_ready()
            input('Press Enter to stop recording...')
        print(f"Recorded {recorder.event_count} events")
    """

    def __init__(
        self,
        capture_dir: str,
        task_description: str = "",
        *,
        capture_video: bool | None = None,
        capture_audio: bool | None = None,
        capture_images: bool | None = None,
        capture_window_data: bool | None = None,
        capture_full_video: bool | None = None,
        video_encoding: str | None = None,
        video_pixel_format: str | None = None,
        stop_sequences: list[list[str]] | None = None,
        log_memory: bool | None = None,
        plot_performance: bool | None = None,
        screen_capture_fps: float | None = None,
        ax_query_interval: float | None = None,
        ax_max_depth: int | None = None,
        ax_dump_timeout: float | None = None,
        ax_element_timeout: float | None = None,
        send_profile: bool = False,
        video_chunk_duration: float | None = None,
        screen_filter: Any | None = None,
    ) -> None:
        from pathlib import Path

        from sc_engine.config import RecordingConfig

        self.capture_dir = str(Path(capture_dir).resolve())
        self.task_description = task_description
        self._send_profile = send_profile
        self._screen_filter = screen_filter

        # Build recording config from constructor params
        self._recording_config = RecordingConfig(
            capture_video=capture_video,
            capture_audio=capture_audio,
            capture_images=capture_images,
            capture_window_data=capture_window_data,
            capture_full_video=capture_full_video,
            video_encoding=video_encoding,
            video_pixel_format=video_pixel_format,
            stop_sequences=stop_sequences,
            log_memory=log_memory,
            plot_performance=plot_performance,
            screen_capture_fps=screen_capture_fps,
            ax_query_interval=ax_query_interval,
            ax_max_depth=ax_max_depth,
            ax_dump_timeout=ax_dump_timeout,
            ax_element_timeout=ax_element_timeout,
            video_chunk_duration=video_chunk_duration,
        )

        # Shared state for cross-thread communication
        self._terminate_processing = multiprocessing.Event()
        self._terminate_recording = multiprocessing.Event()
        self._num_action_events = multiprocessing.Value("i", 0)
        self._num_screen_events = multiprocessing.Value("i", 0)
        self._num_window_events = multiprocessing.Value("i", 0)
        self._num_video_events = multiprocessing.Value("i", 0)

        # Status communication
        self._status_recv, self._status_send = multiprocessing.Pipe(duplex=False)
        self._ready_event = threading.Event()
        self._stopped_event = threading.Event()

        # Chunked recording queues and sync primitives
        self._chunk_rotate_q = None
        self._audio_rotate_q = None
        self._audio_ack_q = None
        self._chunk_process_q = None
        self._flush_requested = None
        self._flush_ack_counter = None

        # Health monitoring — written by _drain_status_pipe thread, read by
        # CLI thread via child_crashes/health_warning properties.  GIL-safe:
        # list.append() and list() copy are atomic bytecode ops in CPython.
        self._child_crashes: list[dict] = []
        self._health_warning: str = ""

        # Internal
        self._record_thread: threading.Thread | None = None
        self._status_thread: threading.Thread | None = None
        self._fanout_thread: threading.Thread | None = None
        self._capture = None  # lazy CaptureSession

    def _drain_status_pipe(self) -> None:
        """Background thread that reads status messages from record()."""
        try:
            while not self._stopped_event.is_set():
                if self._status_recv.poll(timeout=0.5):
                    msg = self._status_recv.recv()
                    if isinstance(msg, dict):
                        if msg.get("type") == "record.started":
                            self._ready_event.set()
                        elif msg.get("type") == "record.stopped":
                            self._stopped_event.set()
                        elif msg.get("type") == "record.child_died":
                            self._child_crashes.append(msg)
                            name = msg["task_name"]
                            code = msg.get("exitcode")
                            if msg.get("is_critical"):
                                self._health_warning = (
                                    f"\u26a0 {name} crashed (exit {code}) "
                                    f"\u2014 stopping"
                                )
                            else:
                                degraded = [
                                    c["task_name"]
                                    for c in self._child_crashes
                                    if not c.get("is_critical")
                                ]
                                self._health_warning = (
                                    f"\u26a0 {', '.join(degraded)} crashed "
                                    f"\u2014 recording degraded"
                                )
        except (EOFError, OSError):
            pass

    def _run_record(self) -> None:
        """Thread target: apply config overrides, then call record()."""
        from sc_engine.config import config_override

        with config_override(self._recording_config):
            record(
                task_description=self.task_description,
                capture_dir=self.capture_dir,
                terminate_processing=self._terminate_processing,
                terminate_recording=self._terminate_recording,
                status_pipe=self._status_send,
                num_action_events=self._num_action_events,
                num_screen_events=self._num_screen_events,
                num_window_events=self._num_window_events,
                num_video_events=self._num_video_events,
                send_profile=self._send_profile,
                recording_config=self._recording_config,
                chunk_rotate_q=self._chunk_rotate_q,
                flush_requested=self._flush_requested,
                flush_ack_counter=self._flush_ack_counter,
                audio_rotate_q=self._audio_rotate_q,
                audio_ack_q=self._audio_ack_q,
                screen_filter=self._screen_filter,
            )

    def _forward_fanout_msg(self, msg) -> None:
        """Forward a single chunk event to audio and chunk processor queues."""
        try:
            if self._audio_rotate_q is not None:
                self._audio_rotate_q.put(msg, timeout=5)
        except Exception:
            logger.error("Fan-out: audio_rotate_q full or dead")
        try:
            if self._chunk_process_q is not None:
                self._chunk_process_q.put(msg, timeout=5)
        except Exception:
            logger.error("Fan-out: chunk_process_q full or dead")

    def _chunk_fanout(self) -> None:
        """Fan-out thread: dispatch chunk rotation events to audio + chunk processor."""
        while not self._stopped_event.is_set():
            try:
                msg = self._chunk_rotate_q.get(timeout=1.0)
            except Exception:
                continue
            self._forward_fanout_msg(msg)
        # Drain remaining messages after stop signal so the final_chunk
        # is never lost due to the _stopped_event race.
        while True:
            try:
                msg = self._chunk_rotate_q.get_nowait()
            except Exception:
                break
            self._forward_fanout_msg(msg)

    def __enter__(self) -> "Recorder":
        # Set up chunking primitives if chunking enabled
        chunk_duration = getattr(self._recording_config, 'video_chunk_duration', None)
        if chunk_duration is None:
            chunk_duration = config.VIDEO_CHUNK_DURATION
        if chunk_duration > 0:
            self._chunk_rotate_q = multiprocessing.Queue(maxsize=100)
            self._audio_rotate_q = multiprocessing.Queue(maxsize=100)
            self._audio_ack_q = multiprocessing.Queue(maxsize=100)
            self._chunk_process_q = multiprocessing.Queue(maxsize=100)
            self._flush_requested = multiprocessing.Event()
            self._flush_ack_counter = multiprocessing.Value('i', 0)

            # Auto-set skip_post_process and screenshot_min_interval for chunked mode
            if self._recording_config.skip_post_process is None:
                self._recording_config.skip_post_process = True
            if self._recording_config.screenshot_min_interval is None:
                self._recording_config.screenshot_min_interval = 1.0

        # Start status drain thread
        self._status_thread = threading.Thread(
            target=self._drain_status_pipe, daemon=True,
        )
        self._status_thread.start()

        # Start fan-out thread if chunking is enabled
        if self._chunk_rotate_q is not None:
            self._fanout_thread = threading.Thread(
                target=self._chunk_fanout, daemon=True,
                name="chunk_fanout",
            )
            self._fanout_thread.start()

        # Start recording thread
        self._record_thread = threading.Thread(target=self._run_record)
        self._record_thread.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self._terminate_processing.set()
        if self._record_thread is not None:
            self._record_thread.join(timeout=30)
            if self._record_thread.is_alive():
                logger.warning("Record thread did not exit in 30s, continuing cleanup")
        self._stopped_event.set()  # ensure status/fanout threads exit
        if self._fanout_thread is not None:
            self._fanout_thread.join(timeout=10)
        if self._status_thread is not None:
            self._status_thread.join(timeout=5)

        # Clean up multiprocessing queues to prevent feeder-thread hangs at exit.
        for q in (self._chunk_rotate_q, self._audio_rotate_q,
                  self._audio_ack_q, self._chunk_process_q):
            if q is not None:
                try:
                    q.cancel_join_thread()
                    q.close()
                except Exception:
                    pass

    def stop(self) -> None:
        """Stop recording programmatically."""
        self._terminate_processing.set()

    def wait_for_ready(self, timeout: float = 60) -> bool:
        """Block until all recording threads/processes have started.

        Returns True if ready, False if timeout expired.
        """
        return self._ready_event.wait(timeout=timeout)

    @property
    def is_recording(self) -> bool:
        """Whether recording is currently active."""
        return (
            self._record_thread is not None
            and self._record_thread.is_alive()
            and not self._terminate_processing.is_set()
        )

    @property
    def health_warning(self) -> str:
        """Human-readable warning if a child process/thread crashed."""
        return self._health_warning

    @property
    def child_crashes(self) -> list[dict]:
        """List of crash events from child processes/threads."""
        return list(self._child_crashes)

    @property
    def event_count(self) -> int:
        """Number of action events recorded so far (or total after stop)."""
        return self._num_action_events.value

    @property
    def screen_count(self) -> int:
        """Number of screen events recorded."""
        return self._num_screen_events.value

    @property
    def video_frame_count(self) -> int:
        """Number of video frames written."""
        return self._num_video_events.value

    @property
    def stats(self) -> dict:
        """Recording statistics snapshot."""
        return {
            "action_events": self._num_action_events.value,
            "screen_events": self._num_screen_events.value,
            "window_events": self._num_window_events.value,
            "video_frames": self._num_video_events.value,
            "is_recording": self.is_recording,
        }

    @property
    def capture(self):
        """Load the CaptureSession after recording completes.

        Returns None if recording has not finished yet.
        """
        if self._capture is None and not self.is_recording:
            try:
                from sc_engine.capture import CaptureSession

                self._capture = CaptureSession.load(self.capture_dir)
            except FileNotFoundError:
                return None
        return self._capture


# Entry point
def start() -> None:
    """Starts the recording process."""
    fire.Fire(record)


if __name__ == "__main__":
    fire.Fire(record)
