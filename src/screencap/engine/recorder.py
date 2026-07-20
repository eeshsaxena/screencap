"""Script for creating Recordings.

Multi-process recording system.
adaptation for per-capture databases.

Usage:

    $ python -m screencap.engine.recorder "<description of task>"

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
from typing import TYPE_CHECKING, Any, Callable, Optional

import av
import fire
import numpy as np
import psutil
from loguru import logger
from pympler import tracker
from pynput import keyboard, mouse
from tqdm import tqdm

from screencap.engine import audio_mute, utils, video, window
from screencap.engine.ax_cache import AXQueryCache
from screencap.engine.config import RecordingConfig, config
from screencap.engine.db import create_db, crud, get_session_for_path
from screencap.engine.db.models import ActionEvent, Recording
from screencap.engine.dedup import dhash, hamming_distance
from screencap.engine.extensions import synchronized_queue as sq
from screencap.engine.retention import RetentionDecision, ScreenRetentionFilter
from screencap.engine.window.ax_browser_url import (
    ALL_KNOWN_BROWSER_BUNDLES as _BROWSER_BUNDLES,
)
from screencap.engine.window.ax_browser_url import (
    extract_browser_url,
    invalidate_url_cache,
)

try:
    import soundfile
except ImportError:
    soundfile = None

if TYPE_CHECKING:
    from screencap._stderr_events import PermissionLabel


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
    pause_state: "_CapturePauseState | None" = None,
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
            # SCR-214 U4: while paused, capture nothing — skip the settle-frame
            # save and the cloud-intent placeholder push so the paused span holds
            # no frames at all (AE1).
            if pause_state is not None and pause_state.paused:
                continue
            # Check settle deadline — force-save current screen as settle frame
            if (retention_filter is not None
                    and prev_screen_event is not None
                    and retention_filter.check_settle(time.monotonic())):
                # Privacy filter: suppress settle frame for blocked apps
                _settle_disp = (
                    screen_filter.get_capture_disposition(prev_screen_event.timestamp)
                    if screen_filter is not None else None
                )
                _settle_screen_ok = _settle_disp.screen_allowed if _settle_disp is not None else True
                _settle_video_ok = _settle_disp.video_allowed if _settle_disp is not None else True
                if not _settle_screen_ok:
                    _drops["privacy_settle_blocked"] += 1
                else:
                    _drops["screen_settle_save"] += 1
                    # Fan-out: save screen; only save video if video is allowed
                    settle_events = [
                        (prev_screen_event, screen_write_q, write_screen_event),
                    ]
                    if config.RECORD_VIDEO and not config.RECORD_FULL_VIDEO and _settle_video_ok:
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
        # SCR-214 U4: while paused, drop the event unprocessed — no writer sees
        # it, so the paused span records NOTHING (a genuine capture gap → "nothing
        # captured", AE1). This is the AUTHORITATIVE gate: it also discards any
        # frames queued before the pause landed and prevents action-gated video
        # from saving the last screen frame while paused.
        if pause_state is not None and pause_state.paused:
            continue
        logger.trace(f"{event=}")
        assert event.type in EVENT_TYPES, event
        # Drain the override queue on EVERY event (not just window
        # events) so a menubar Disable toggle takes effect within one
        # event cycle even when the user stays on the same tab. The
        # filter re-evaluates its cached window state internally when a
        # new override is drained, updating the blocked state on the
        # spot.
        if screen_filter is not None:
            try:
                screen_filter.poll_overrides()
            except Exception:
                _drops["privacy_filter_error"] += 1
                screen_filter.fail_closed()
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
            # Mask sensitive background window regions (e.g. Slack visible
            # behind the active window) before the frame reaches any writer.
            # mask_frame() runs for both local and cloud recordings; it masks
            # at the user's configured mode for local and at PUBLIC for
            # cloud-intent. It only masks windows classified EXCLUDE/MASK_WINDOW
            # and is a no-op when geometry has no windows.
            if screen_filter is not None and hasattr(screen_filter, "mask_frame"):
                screen_filter.mask_frame(
                    event.data, event.extra, recording.pixel_ratio,
                )
            prev_screen_event = event
            if config.RECORD_FULL_VIDEO:
                # Privacy filter: skip full-video frames for blocked apps
                # Uses video_allowed (blocks for both EXCLUDE and MASK_WINDOW)
                _full_vid_disp = screen_filter.get_capture_disposition(event.timestamp) if screen_filter is not None else None
                if _full_vid_disp is not None and not _full_vid_disp.video_allowed:
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
                _gated_vid_disp = screen_filter.get_capture_disposition(event.timestamp) if screen_filter is not None else None
                if _gated_vid_disp is not None and not _gated_vid_disp.video_allowed:
                    _push_placeholder_if_due(event.timestamp, event.data)
                else:
                    _end_blocked_interval_if_active(event.timestamp)
        elif event.type == "window":
            prev_window_event = event
            # Notify privacy filter of window change. poll_overrides
            # already ran at the top of the loop, so on_window_event
            # will see any pending overrides.
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

            # Cache privacy disposition for this action event (avoids redundant lock + FFI)
            _disposition = screen_filter.get_capture_disposition(prev_screen_event.timestamp) if screen_filter is not None else None
            _screen_allowed = _disposition.screen_allowed if _disposition is not None else True

            # Privacy filter: suppress screenshot for blocked apps (EXCLUDE only)
            if should_save_screen and _disposition is not None:
                if not _disposition.screen_allowed:
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
                # Video: use video_allowed gate (blocks for MASK_WINDOW + EXCLUDE)
                _video_ok = _disposition.video_allowed if _disposition is not None else True
                if config.RECORD_VIDEO and not config.RECORD_FULL_VIDEO and _video_ok:
                    video_event = prev_screen_event._replace(type="screen/video")
                    events_to_write.append(
                        (video_event, video_write_q, write_video_event)
                    )
            if prev_window_event is not None:
                if prev_saved_window_timestamp < prev_window_event.timestamp:
                    events_to_write.append(
                        (prev_window_event, window_write_q, write_window_event)
                    )
            # Privacy filter: null sensitive content for blocked/masked apps.
            # Uses keystrokes_allowed (nulls for both EXCLUDE and MASK_WINDOW).
            if _disposition is not None and not _disposition.keystrokes_allowed:
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


# Per-process cache of the corpus key for encrypted capture (search U2). Loaded
# once, lazily, from the daemon-supplied ``SCREENCAP_CORPUS_KEY_FILE`` (never argv —
# the bytes must not be ``ps``-visible). Spawn-safe: a re-imported child resets this
# to (False, None) and re-reads the inherited env file on its first still.
_CORPUS_KEY_LOADED = False
_CORPUS_KEY_CACHE: bytes | None = None


def _corpus_key() -> bytes | None:
    """Return the corpus key for encrypted capture, or ``None`` if unavailable.

    Read-only + cached: the writer never mints a key (the daemon does, before it
    enables encryption). A ``None`` here makes the write path fail closed rather
    than emit plaintext."""
    global _CORPUS_KEY_LOADED, _CORPUS_KEY_CACHE
    if not _CORPUS_KEY_LOADED:
        try:
            from screencap import corpus_crypto

            _CORPUS_KEY_CACHE = corpus_crypto.load_corpus_key()
        except Exception:  # noqa: BLE001 — any key-load failure = "no key" → fail closed
            logger.error("Failed to load the corpus key for encrypted capture", exc_info=True)
            _CORPUS_KEY_CACHE = None
        _CORPUS_KEY_LOADED = True
    return _CORPUS_KEY_CACHE


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
        # Corpus encryption (search R3): when the U8 gate flips RECORD_IMAGES_ENCRYPTED
        # on, stills persist as AES-256-GCM ``.jpg.enc`` (and blobs are encrypted)
        # instead of plaintext. Fail closed — never write plaintext when encryption
        # is required but the key is missing (the gate should have forced stills off;
        # this is the defense-in-depth guard that upholds the never-write-plaintext
        # invariant). Default OFF preserves today's plaintext behavior byte-for-byte.
        encrypt = config.RECORD_IMAGES_ENCRYPTED
        key = _corpus_key() if encrypt else None
        if encrypt and key is None:
            logger.error(
                "Corpus encryption is enabled but the corpus key is unavailable; "
                "skipping this still (no plaintext written)"
            )
        elif screenshots_dir:
            import math

            ts = event.timestamp
            if math.isfinite(ts) and ts > 0:
                filename = f"{ts:.6f}.jpg"
                if encrypt:
                    from screencap import still_io

                    enc_path = os.path.join(screenshots_dir, filename + still_io.ENC_SUFFIX)
                    try:
                        with io.BytesIO() as output:
                            image.save(
                                output, format="JPEG", quality=config.SCREENSHOT_JPEG_QUALITY
                            )
                            jpeg_bytes = output.getvalue()
                        still_io.write_encrypted_still(enc_path, jpeg_bytes, key)
                        event_data["image_path"] = f"screenshots/{filename}{still_io.ENC_SUFFIX}"
                    except OSError:
                        logger.warning(
                            f"Failed to save encrypted screenshot to {enc_path}, skipping"
                        )
                else:
                    file_path = os.path.join(screenshots_dir, filename)
                    try:
                        image.save(
                            file_path, format="JPEG", quality=config.SCREENSHOT_JPEG_QUALITY
                        )
                        os.chmod(file_path, 0o600)
                        event_data["image_path"] = f"screenshots/{filename}"
                    except OSError:
                        logger.warning(
                            f"Failed to save screenshot to {file_path}, skipping"
                        )
        else:
            with io.BytesIO() as output:
                image.save(output, format="JPEG", quality=config.SCREENSHOT_JPEG_QUALITY)
                png_data = output.getvalue()
            if encrypt:
                from screencap import corpus_crypto, still_io

                png_data = corpus_crypto.encrypt(
                    png_data, key, still_io.png_blob_aad(recording.timestamp, event.timestamp)
                )
            event_data["png_data"] = png_data
    crud.insert_screenshot(db, recording, event.timestamp, event_data)

    # Persist window geometry alongside the screenshot (if captured).
    #
    # This is the input the post-hoc video masker (U6) uses to mask
    # sensitive windows out of the cloud-bound video copy. It is recorded
    # for EVERY destination — the capture path is destination-agnostic; the
    # bounds + ``event.timestamp`` are queryable for U6's per-frame gap
    # analysis. Capture cadence/mechanics are unchanged.
    #
    # The insert is best-effort (a geometry-write hiccup must never abort a
    # recording), but a silent failure is dangerous: video frames are
    # continuous while geometry is sampled sparsely, so a swallowed insert
    # leaves a span of frames with NO recorded bounds, which U6 cannot
    # distinguish from "no sensitive window" — it would emit an
    # effectively-unmasked cloud video that looks fine. So on failure we now
    # (1) log LOUDLY (warning, not debug) and (2) write a durable, timestamped
    # marker so U6 can fail closed on the affected chunk span (case b).
    window_geometries = event.extra
    if window_geometries is not None:
        try:
            crud.insert_window_geometry(
                db, recording, event.timestamp, json.dumps(window_geometries),
            )
        except Exception as geom_exc:
            logger.warning(
                "Failed to insert window geometry at ts={} ({}); recording "
                "a durable capture-failure marker so post-hoc video masking "
                "(U6) fails closed on this span",
                event.timestamp,
                type(geom_exc).__name__,
                exc_info=True,
            )
            # Durable, crash-surviving signal — but still best-effort: a
            # marker-write failure must not abort the recording either. Log
            # it loudly rather than swallowing silently.
            try:
                crud.insert_window_geometry_capture_failure(
                    db,
                    recording,
                    event.timestamp,
                    detail=f"{type(geom_exc).__name__}: {geom_exc}",
                )
            except Exception:
                logger.warning(
                    "Failed to record window-geometry capture-failure marker "
                    "at ts={}; U6 cannot see this gap",
                    event.timestamp,
                    exc_info=True,
                )

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
    from screencap.engine.config import apply_config_overrides
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


def _network_event_to_db_dict(event: Any, kind: str) -> dict[str, Any]:
    """Convert a Pydantic network event to the dict shape ``crud.insert_network_event`` expects.

    Pydantic events carry ``body_sha256_hex: str | None``; the DB column is
    raw 32 bytes. Pydantic ``headers: list[tuple[str, str]]`` is JSON-encoded
    into ``headers_json``. Per-kind fields (status / method / direction /
    frame_type) are looked up only if present.

    For ``ws_upgrade``: response headers go to ``headers_json``, request
    headers (if present in ``details_json``) stay in ``details_json``.
    For ``drop_burst``: ``flow_id`` is None (no associated flow).

    V1.5 body-encryption fields (``body_ciphertext`` / ``body_nonce`` /
    ``body_aad``) flow through ``getattr`` like any other column. The
    addon populates them on encrypted body events; the writer persists
    them so the export-time scrub pipeline can decrypt at read time.
    Without this passthrough, encrypted bodies would arrive with all
    three columns NULL — silently breaking the entire V1.5 contract.
    """
    from screencap.engine.convert import hex_to_bytes

    headers_pairs = list(getattr(event, "headers", []) or [])
    sha_hex = getattr(event, "body_sha256_hex", None)
    sha_bytes = hex_to_bytes(sha_hex) if sha_hex else None

    details = getattr(event, "details_json", None)
    # V1.5 network.tunneled has its own payload shape; lift to details_json
    # so the existing storage column carries it forward without a new column.
    if kind == "tunneled":
        details = {
            "started_at": getattr(event, "started_at", 0.0),
            "duration_seconds": getattr(event, "duration_seconds", 0.0),
        }
    out: dict[str, Any] = {
        "kind": kind,
        "flow_id": getattr(event, "flow_id", None),
        "method": getattr(event, "method", None),
        "url": getattr(event, "url", None),
        "host": getattr(event, "host", None),
        "status": getattr(event, "status", None),
        "headers_json": json.dumps(headers_pairs) if headers_pairs else None,
        "body_size": getattr(event, "body_size", None),
        "body_sha256": sha_bytes,
        "body_ciphertext": getattr(event, "body_ciphertext", None),
        "body_nonce": getattr(event, "body_nonce", None),
        "body_aad": getattr(event, "body_aad", None),
        "content_type": getattr(event, "content_type", None),
        "direction": getattr(event, "direction", None),
        "frame_type": getattr(event, "frame_type", None),
        "http_version": getattr(event, "http_version", None),
        "details_json": json.dumps(details) if details else None,
        "timestamp": getattr(event, "timestamp", 0.0),
        "timestamp_ns": getattr(event, "timestamp_ns", 0),
    }
    return out


def write_network_events(
    write_q: sq.SynchronizedQueue,
    num_events: multiprocessing.Value,
    perf_q: sq.SynchronizedQueue,
    recording: Recording,
    db_path: str,
    terminate_processing: multiprocessing.Event,
    started_event: multiprocessing.Event,
    config_overrides: dict[str, object] | None = None,
    flush_requested=None,
    flush_ack_counter=None,
) -> None:
    """Writer process for network events.

    Distinct from :func:`write_events` because it must handle five subtypes
    (``network.request`` / ``network.response`` / ``network.ws_upgrade`` /
    ``network.ws_frame`` / ``network.drop_burst``) sharing one queue, and
    cannot use the ``assert event.type == event_type`` check from
    :func:`write_events` (which would fail on every dotted type name).

    Persists each event to ``recording.db.network_event`` via
    :func:`crud.insert_network_event``. The DB ``kind`` column gets the
    short form (``request``, ``response``, etc.) -- the dotted form lives
    only on the Pydantic event for events.jsonl emission (V1.75).

    The ``terminate_processing`` parameter here is bound to a DEDICATED
    network-writer terminate event (set in ``_setup_network_capture``),
    NOT the global ``terminate_processing`` shared by other writers.
    The global event fires before ``_teardown_network_capture`` runs,
    but the addon's ``done()`` hook emits final events (notably
    ``network.tunneled``) only AFTER the proxy receives SIGTERM —
    partway through teardown. Those events flow proxy → reader →
    ``write_q`` and need a live writer to drain them.
    ``_teardown_network_capture`` sets the dedicated event AFTER the
    reader thread joins, so the writer drains every event the reader
    forwarded before exiting.
    """
    from screencap.engine.config import apply_config_overrides
    apply_config_overrides(config_overrides)

    utils.set_start_time(recording.timestamp)

    logger.info("network_event_writer starting")
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    crud.BATCH_SIZE = 50
    session = get_session_for_path(db_path)

    # Maps the EventType.value (dotted form) to the DB `kind` short form.
    _KIND_MAP = {
        "network.request": "request",
        "network.response": "response",
        "network.ws_upgrade": "ws_upgrade",
        "network.ws_frame": "ws_frame",
        "network.drop_burst": "drop_burst",
        "network.tunneled": "tunneled",
    }

    started = False
    num_processed = 0
    try:
        while not terminate_processing.is_set() or not write_q.empty():
            if not started:
                started_event.set()
                started = True
            try:
                event = write_q.get_nowait()
            except queue.Empty:
                # Mid-recording flush hook (chunked mode).
                if flush_requested is not None and flush_requested.is_set():
                    crud.flush_buffers(session)
                    with flush_ack_counter.get_lock():
                        flush_ack_counter.value += 1
                time.sleep(0.01)
                continue
            try:
                event_type_value = (
                    event.type.value if hasattr(event.type, "value") else event.type
                )
            except AttributeError:
                logger.debug("network writer: dropping event without type field: %r", event)
                continue
            kind = _KIND_MAP.get(event_type_value)
            if kind is None:
                logger.debug(
                    "network writer: dropping event with unknown type %r", event_type_value
                )
                continue
            try:
                event_dict = _network_event_to_db_dict(event, kind)
                crud.insert_network_event(session, recording, event_dict)
            except Exception as exc:  # noqa: BLE001 — never let one bad row kill the writer
                logger.warning("network writer: insert failed for kind=%s: %s", kind, exc)
                continue
            num_processed += 1
            with num_events.get_lock():
                num_events.value += 1
    finally:
        crud.flush_buffers(session)

    logger.info(f"network_event_writer done; processed={num_processed}")


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
    chunk_duration: float = 900.0, chunk_rotate_q=None,
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
        codec=config.VIDEO_ENCODING,
        pix_fmt=config.VIDEO_PIXEL_FORMAT,
        crf=config.VIDEO_CRF,
        preset=config.VIDEO_PRESET,
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
    _video_start_updated: bool = False,
    **kwargs: dict,
) -> dict[str, Any]:
    """Write a screen event using ChunkedVideoWriter."""
    assert event.type == "screen/video"
    screenshot_image = event.data
    screenshot_timestamp = event.timestamp
    chunked_writer.write_frame(screenshot_image, screenshot_timestamp)

    # Update DB video_start_time to the actual first frame's timestamp
    # so that CaptureSession.get_frame_at() computes correct offsets.
    if not _video_start_updated and chunked_writer.start_time is not None:
        crud.update_video_start_time(db, recording_timestamp, chunked_writer.start_time)
        video_start_timestamp = chunked_writer.start_time
        _video_start_updated = True

    return {
        **kwargs,
        "chunked_writer": chunked_writer,
        "video_start_timestamp": video_start_timestamp,
        "_video_start_updated": _video_start_updated,
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

    Returns:
        dict containing state.
    """
    assert event.type == "screen/video"
    screenshot_image = event.data
    screenshot_timestamp = event.timestamp
    force_key_frame = last_pts == 0
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

    # Capture-health (SCR-76): the action reader produced an event. Count it
    # before the enqueue branch — a queue.Full drop still counts as output
    # (the reader IS alive and producing; the drop is downstream backpressure).
    _health_incr("action.output")

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
    pause_state: "_CapturePauseState | None" = None,
) -> None:
    """Read screen events and add them to the event queue.

    Captures at most ``config.SCREEN_CAPTURE_FPS`` frames per second.
    Set to 0 for unlimited (legacy behaviour).

    While ``pause_state`` reports paused (SCR-214 U4), the screenshot grab is
    skipped entirely — no frame is captured, so the paused span records nothing
    (AE1). ``process_events`` is the authoritative gate (it also drops any in-
    flight frames), but skipping the capture here avoids taking a screenshot only
    to discard it, and keeps the health counters honest (no attempt is counted).

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
    _display_bounds = None
    try:
        from screencap.engine.window._macos import (
            get_all_window_geometries,
            get_main_display_bounds,
        )
        _get_geometries = get_all_window_geometries
        _display_bounds = get_main_display_bounds()  # (origin_x, origin_y, width, height)
    except (ImportError, OSError):
        pass

    logger.info(f"Starting (fps={fps}, min_interval={min_interval:.3f}s)")
    started = False
    _geom_slow_count = 0
    while not terminate_processing.is_set():
        # Capture-pause (SCR-214 U4): while paused, capture nothing — skip the
        # screenshot grab (and its health-attempt count) so the paused span is a
        # genuine gap, not discarded footage. ``started_event`` is set on the
        # first UNPAUSED frame; a recording paused before its first frame simply
        # idles here until resumed.
        if pause_state is not None and pause_state.paused:
            terminate_processing.wait(timeout=max(min_interval, 0.1))
            continue
        # Capture-health (SCR-76): count every attempt before the capture
        # call; count output only for a non-None frame. A None/exception is
        # the only robust "screen reader is broken" content signal — denial
        # may yield a changing wallpaper frame, so frame content is NOT relied
        # on (see _capture_health_step + plan Key Technical Decisions).
        _health_incr("screen.attempt")
        t_start = time.perf_counter()
        screenshot = utils.take_screenshot()
        t_screenshot = time.perf_counter()
        if screenshot is None:
            logger.warning("Screenshot was None")
            # SCR-103: a sleeping/locked display legitimately yields no frame —
            # benign idle, not a stalled reader. Count it as a completed (idle)
            # capture so it does not widen the attempt-vs-output gap that fires
            # capture_unhealthy(reader=screen), and back off so a fast-failing
            # screencapture does not spin the attempt counter. A None while the
            # display is ACTIVE is a genuine capture failure (incl. Screen-
            # Recording denial) and still trips the stall verdict + labeller.
            if utils.display_is_asleep():
                _health_incr("screen.output")
                # Review (#205): back off to an idle cadence independent of
                # min_interval so a fps<=0 (uncapped) recording cannot busy-spin
                # screencapture while the display sleeps.
                time.sleep(max(min_interval, 1.0))
            continue
        _health_incr("screen.output")

        # Capture window geometry immediately after screenshot for accurate bounds
        window_geometries = None
        if _get_geometries is not None:
            t_geom_start = time.perf_counter()
            try:
                _win_list = _get_geometries()
                # Bundle display bounds with window list for multi-monitor support
                window_geometries = {
                    "windows": _win_list,
                    "display_bounds": _display_bounds,
                }
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
        # Capture-health (SCR-76): count every poll attempt; count output when
        # the poll completes, BEFORE the change gate below — so a user sitting on
        # one unchanged window stays healthy.
        _health_incr("window.attempt")
        window_data = window.get_active_window_data()
        if not window_data:
            # SCR-103: a falsy poll is a benign no-active-window state (bare
            # desktop, Mission Control, Spotlight, menu-bar/Space focus), not an
            # Accessibility stall — the window meta rides CGWindowList, which
            # needs no *Accessibility* permission (it is Screen-Recording-gated,
            # NOT permission-free; see SCR-101). Count it as a completed (idle)
            # poll so it does not widen the attempt-vs-output gap that fires
            # capture_unhealthy(reader=window).
            #
            # SCR-108 tradeoff: because every poll — falsy or truthy — increments
            # window.output, the stall verdict (attempt > 0 and output == 0) can
            # never fire for the window reader. A benign idle poll and an
            # alive-but-blind reader (sustained falsy polls from a real
            # degradation) are indistinguishable at this layer, so detection is
            # consciously left to record.child_died — which catches a DEAD reader
            # thread but NOT an alive-but-blind one. Accepted because
            # capture_unhealthy is advisory / fail-open (it never self-stops).
            _health_incr("window.output")
            time.sleep(poll_interval)
            continue
        _health_incr("window.output")

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
                # Always invalidate the URL cache for browser windows.
                # Tab switches keep the same window_id and sometimes
                # the same kCGWindowName (Chrome returns empty name),
                # so the only reliable way to detect tab changes is to
                # re-query the address bar on every poll cycle (~5ms).
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
            #   File "screencap.engine/recorder.py" in read window events
            #   File "...\env\lib\site-packages\loguru\logger.py" line 1977, in info
            #   File "...\env\lib\site-packages\loguru\_logger.py", line 1964, in _log
            #       for handler in core.handlers.values):
            #   RuntimeError: dictionary changed size during iteration
            _window_data = {k: v for k, v in window_data.items() if k not in ("state", "browser_url")}
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

    from screencap.engine.platform import get_display_pixel_ratio

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
    # Capture-health (SCR-76): publish the listener handle so record()'s
    # supervisor can sample its liveness (_action_listener_alive). A missing
    # handle is treated as alive (fail-open), so the start race is harmless.
    _listener_handles["keyboard"] = keyboard_listener

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
    # Capture-health (SCR-76): publish the listener handle for liveness
    # sampling by record()'s supervisor (fail-open if absent).
    _listener_handles["mouse"] = mouse_listener

    # NOTE: listener may not have actually started by now
    # TODO: handle race condition, e.g. by sending synthetic events from main thread
    started_event.set()

    terminate_processing.wait()
    mouse_listener.stop()


# Shared drop counters: written by reader threads, gesture tap, process_events.
# Python's GIL makes dict operations safe enough for counters (minor undercount
# possible on truly concurrent increments, acceptable for observability data).
_drop_counts: dict = {}

# Capture-health detection (SCR-76) -----------------------------------------
# Raw per-reader attempt/output counters, DISTINCT from the post-filter
# num_*_events (which count action-gated DB commits, not raw reader output).
# Written by the reader threads + the action trigger; snapshotted by record()'s
# supervisor loop. Plain ints, GIL-safe (mirrors _drop_counts). Keys:
#   screen.attempt / screen.output, window.attempt / window.output, action.output
# Reset per recording in record().
_capture_health_counts: dict = {}

# Action-reader listener handles, published by the keyboard/mouse/gesture
# readers after .start() so record()'s supervisor can sample their liveness.
# A missing/None handle is treated as alive (fail-open), so the start race
# never produces a false unhealthy verdict. Reset per recording in record().
_listener_handles: dict = {}


def _health_incr(key: str) -> None:
    """Increment an in-process capture-health counter (SCR-76).

    GIL-safe plain-dict increment, mirroring the ``_drop_counts`` pattern. A
    minor undercount on truly concurrent increments is acceptable for this
    observability signal.
    """
    _capture_health_counts[key] = _capture_health_counts.get(key, 0) + 1


def _action_listener_alive() -> bool:
    """Best-effort liveness for the callback-driven action readers (SCR-76).

    Returns ``True`` (alive) unless EVERY published, sampleable listener handle
    positively reports not-running. No handles yet, a ``None`` handle, or any
    sampling error is treated as alive (fail-open), so the start race and
    transient PyObjC hiccups never produce a false unhealthy verdict.

    KNOWN LIMITATION (documented coverage gap, SCR-76 plan): a pynput
    ``Listener.running`` stays ``True`` under TCC callback starvation (Input
    Monitoring / Accessibility denied → zero callbacks delivered). This
    heartbeat therefore catches a crashed/stopped listener on a live thread,
    NOT a silently-starved one.
    """
    handles = list(_listener_handles.items())
    if not handles:
        return True  # nothing published yet — fail-open (start race)
    saw_sample = False
    for name, handle in handles:
        if handle is None:
            continue
        try:
            if name == "gesture":
                import Quartz
                running = bool(Quartz.CGEventTapIsEnabled(handle))
            else:
                running = bool(getattr(handle, "running"))
        except Exception:
            return True  # sampling error — fail-open
        saw_sample = True
        if running:
            return True  # at least one action listener is alive
    # Reached only when every sampled handle reported not-running.
    return not saw_sample


def _probe_tcc_denied(reader: str | None = None) -> "PermissionLabel | None":
    """In-process TCC attribution for an observed capture-health symptom (SCR-76).

    Returns the ``permission`` label of the first permission that reports
    DENIED — ``"screen_recording"`` / ``"input_monitoring"`` / ``"accessibility"``
    — or ``None`` when attribution is inconclusive (Quartz/AX import or call
    fails, or every permission reports granted, including a stale "granted"
    from the per-process TCC cache).

    Calls the Quartz / ApplicationServices primitives DIRECTLY rather than
    ``DarwinPlatform.is_accessibility_enabled()``, whose ``osascript`` subprocess
    fallback would block the 1s supervisor loop for up to 5s and reintroduce a
    subprocess into the path this design keeps subprocess-free. Fail-open: any
    error → ``None``; never raises.

    The check ORDER is biased by which reader is unhealthy so the returned
    label is the most likely cause when more than one permission is denied;
    the unhealth verdict itself comes from the counter gap, not this labeller.
    """
    if sys.platform != "darwin":
        return None
    from screencap._stderr_events import (
        PERMISSION_ACCESSIBILITY,
        PERMISSION_INPUT_MONITORING,
        PERMISSION_SCREEN_RECORDING,
    )
    try:
        import Quartz
    except Exception:
        return None

    def _screen() -> "PermissionLabel | None":
        try:
            return PERMISSION_SCREEN_RECORDING if Quartz.CGPreflightScreenCaptureAccess() is False else None
        except Exception:
            return None

    def _input() -> "PermissionLabel | None":
        try:
            return PERMISSION_INPUT_MONITORING if Quartz.CGPreflightListenEventAccess() is False else None
        except Exception:
            return None

    def _ax() -> "PermissionLabel | None":
        try:
            from ApplicationServices import (
                AXIsProcessTrustedWithOptions,
                kAXTrustedCheckOptionPrompt,
            )
            trusted = AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: False})
            return PERMISSION_ACCESSIBILITY if trusted is False else None
        except Exception:
            return None

    order = {
        "screen": (_screen, _input, _ax),
        "window": (_ax, _screen, _input),
        "action": (_input, _ax, _screen),
    }.get(reader, (_screen, _input, _ax))
    for check in order:
        label = check()
        if label is not None:
            return label
    return None


def _capture_health_step(
    prev: dict,
    cur: dict,
    alive: dict,
    action_alive: bool,
    debounce: int,
    runs: dict,
    emitted: dict,
) -> tuple[list[str], list[str]]:
    """Advance per-reader capture-health debounce state by one supervisor tick.

    Pure decision step (no I/O), unit-testable in isolation from the engine.
    Verdict per reader:
      - **screen / window**: unhealthy when the attempt delta > 0 and the
        useful-output delta == 0 over the tick (attempting but producing
        nothing useful).
      - **action**: unhealthy when ``action_alive`` is ``False`` (callback
        readers have no attempt site, so liveness comes from the heartbeat).

    A reader that is not alive (``alive[reader]`` falsy or absent) is left to
    the existing ``record.child_died`` path and its debounce state is reset, so
    there is no double-emit. Mutates ``runs`` / ``emitted`` in place.

    Returns ``(unhealthy_edges, recovered_edges)``:
      - **unhealthy_edges** — readers that crossed the unhealthy edge THIS tick
        (run length reached ``debounce`` and not already emitted); the caller
        labels + emits ``capture_unhealthy`` (or ``permission_lost``) once.
      - **recovered_edges** — readers that returned healthy THIS tick AFTER a
        surfaced unhealthy edge (``emitted`` was set); the caller emits the
        paired ``capture_recovered`` once (SCR-100) so the shell drops the stale
        advisory instead of holding it until the recording ends. A reader that
        merely blipped below ``debounce`` (never ``emitted``) recovers silently —
        nothing was surfaced, so nothing needs clearing. ``emitted`` is cleared
        on recovery, so a recover-then-rebreak re-emits ``capture_unhealthy``.
        A dead reader is left to ``record.child_died`` and never reported as
        recovered (it did not recover — it died).
    """
    edges: list[str] = []
    recovered: list[str] = []
    for reader in ("screen", "window", "action"):
        if not alive.get(reader):
            runs[reader] = 0
            emitted[reader] = False
            continue
        if reader == "action":
            unhealthy = not action_alive
        else:
            attempt = cur.get(f"{reader}.attempt", 0) - prev.get(f"{reader}.attempt", 0)
            output = cur.get(f"{reader}.output", 0) - prev.get(f"{reader}.output", 0)
            unhealthy = attempt > 0 and output == 0
        if unhealthy:
            runs[reader] = runs.get(reader, 0) + 1
            if runs[reader] >= debounce and not emitted.get(reader):
                emitted[reader] = True
                edges.append(reader)
        else:
            if emitted.get(reader):
                recovered.append(reader)
            runs[reader] = 0
            emitted[reader] = False
    return edges, recovered


def _emit_capture_health_event(
    reader: str, label: "PermissionLabel | None", elapsed: float, emit: Callable[..., None]
) -> str:
    """Emit the right stderr event for a capture-health edge (SCR-76).

    Only a ``screen_recording`` denial is terminal: it reuses the existing
    terminal-capable ``permission_lost`` (so the shell's deny path is preserved
    unchanged), because losing Screen Recording means the core screen capture is
    genuinely dead. Every other outcome is the advisory, non-terminal
    ``capture_unhealthy`` with a closed-set ``reason``: a ``None`` label (non-TCC
    / inconclusive), AND an ``accessibility`` / ``input_monitoring`` label
    (SCR-101). The latter two are best-guess attributions for a window / action
    stall whose true cause is NOT those permissions — the window reader's output
    rides ``CGWindowListCopyWindowInfo`` and the action reader rides the input
    listener, both independent of Accessibility — so acting terminally on that
    guess would self-stop an otherwise-healthy screen+audio recording (the
    capture-health detector is emit-only / fail-open by design). ``emit`` is the
    ``emit_event`` callable, injected so this mapping is unit-testable. Returns
    the emitted event ``type`` string.
    """
    from screencap._stderr_events import (
        CAPTURE_UNHEALTHY_REASON_LISTENER_DEAD,
        CAPTURE_UNHEALTHY_REASON_READER_STALLED,
        EVENT_CAPTURE_UNHEALTHY,
        EVENT_PERMISSION_LOST,
        PERMISSION_SCREEN_RECORDING,
    )
    if label == PERMISSION_SCREEN_RECORDING:
        emit(EVENT_PERMISSION_LOST, permission=label, elapsed=elapsed)
        return EVENT_PERMISSION_LOST
    reason = (
        CAPTURE_UNHEALTHY_REASON_LISTENER_DEAD
        if reader == "action"
        else CAPTURE_UNHEALTHY_REASON_READER_STALLED
    )
    emit(EVENT_CAPTURE_UNHEALTHY, reason=reason, reader=reader, elapsed=elapsed)
    return EVENT_CAPTURE_UNHEALTHY


def _capture_health_tick(
    *,
    prev_counts: dict | None,
    cur_counts: dict,
    elapsed: float,
    window_secs: float,
    debounce: int,
    runs: dict,
    emitted: dict,
    alive: dict,
    action_alive: bool,
    emit: Callable[..., None],
    probe: Callable[[str | None], str | None] = _probe_tcc_denied,
) -> list[tuple[str, str]]:
    """Run one capture-health supervisor tick (SCR-76).

    Warmup-gates (no verdict until ``prev_counts`` exists and ``elapsed`` has
    reached one full ``window_secs``), evaluates per-reader deltas via
    ``_capture_health_step``, attributes each fresh unhealthy edge with ``probe``
    and emits once via ``_emit_capture_health_event``, and emits the paired
    ``capture_recovered`` (SCR-100) once per recovery edge so the shell can drop
    the stale advisory mid-recording. Returns the list of
    ``(reader, event_type)`` emitted this tick.

    The detection → attribution → emission WIRING lives here (not inline in
    ``record()``) so it is exercisable in tests with stubbed ``emit`` / ``probe``
    without spawning the full engine — closing the "green-in-tests,
    broken-in-frozen" gap (R12) without a subprocess. ``emit`` (stderr) and the
    in-process ``probe`` are both frozen-safe by construction.
    """
    from screencap._stderr_events import EVENT_CAPTURE_RECOVERED

    if prev_counts is None or elapsed < window_secs:
        return []  # start-barrier / warmup — establish baseline, no verdict yet

    events: list[tuple[str, str]] = []
    unhealthy, recovered = _capture_health_step(
        prev_counts, cur_counts, alive, action_alive, debounce, runs, emitted,
    )
    for reader in unhealthy:
        label = probe(reader)
        events.append((reader, _emit_capture_health_event(reader, label, elapsed, emit)))
    for reader in recovered:
        # Paired advisory clear (SCR-100): no probe — recovery is unconditional,
        # carries reader + elapsed only (no reason). The shell uses it to drop
        # the stale capture_unhealthy advisory for this reader mid-recording.
        emit(EVENT_CAPTURE_RECOVERED, reader=reader, elapsed=elapsed)
        events.append((reader, EVENT_CAPTURE_RECOVERED))
    return events

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

    # CGEventTap sentinel types — macOS delivers these when a tap is disabled.
    _TAP_DISABLED_BY_TIMEOUT = Quartz.kCGEventTapDisabledByTimeout
    _TAP_DISABLED_BY_USER_INPUT = Quartz.kCGEventTapDisabledByUserInput

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

        # Handle tap-disabled sentinel events before any NSEvent conversion.
        # macOS delivers these when the tap is auto-disabled (e.g. callback
        # took too long or SecureInput activated). Re-enable immediately
        # rather than waiting for the periodic timer (up to 500ms).
        if event_type in (_TAP_DISABLED_BY_TIMEOUT, _TAP_DISABLED_BY_USER_INPUT):
            logger.debug("Gesture event tap disabled (type=%s), re-enabling", event_type)
            Quartz.CGEventTapEnable(tap, True)
            return cg_event

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
    # Capture-health (SCR-76): publish the tap handle so the supervisor can
    # sample CGEventTapIsEnabled(tap) for action liveness. The tap is local to
    # this run-loop thread; a stale handle after teardown is harmless because
    # record() resets the handle map per recording and the heartbeat is
    # fail-open (alive unless EVERY published handle reports not-running).
    _listener_handles["gesture"] = tap

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


def _apply_audio_mute_command(
    controller: "audio_mute.AudioStreamController",
    command: dict,
    *,
    session,
    recording: Recording,
    emit=None,
) -> str | None:
    """Apply one ``set_muted`` command inside ``record_audio`` (SCR-218 U2).

    Confirmed-state (KTD4): the ``muted_intervals`` row is written and the
    ``audio_muted`` / ``audio_unmuted`` event emitted ONLY after the stream
    actually toggled — never on the mere request. A denied / unavailable device
    on unmute emits the advisory ``audio_unmute_failed`` and stays muted (R3:
    unmute never silently fails), leaving any open interval open. Symmetrically
    (SCR-271), a raise DURING the mute toggle (a faulting DB write or a
    ``stream.stop()`` ``PortAudioError``) emits the advisory ``audio_mute_failed``
    rather than propagating — every non-crashing request yields exactly one
    terminal event, so the app's in-flight guard can never strand on "Muting…".

    ``command['ts']`` is the recording-relative timestamp captured at command
    *receipt* in the engine-main handler (before the queue hop), so the interval
    over-covers the ~100 ms stop latency (KTD3); it falls back to a fresh
    ``get_timestamp()`` only if absent. Isolated from ``record_audio``'s
    closures so the confirmed-state ordering is unit-testable with a fake
    controller + in-memory DB.

    Returns the emitted event name. A confirming ``audio_muted`` /
    ``audio_unmuted`` event fires for EVERY (non-failed) request — including an
    idempotent no-op where the stream was already in the requested capture state
    — because the app clears its transitional "Muting…"/"Unmuting…" guard ONLY on
    that event; a swallowed no-op would strand the control forever.
    """
    from screencap._stderr_events import (
        AUDIO_UNMUTE_FAILED_REASON_MIC_UNAVAILABLE,
        EVENT_AUDIO_MUTE_FAILED,
        EVENT_AUDIO_MUTED,
        EVENT_AUDIO_UNMUTE_FAILED,
        EVENT_AUDIO_UNMUTED,
        emit_event,
    )

    if emit is None:
        emit = emit_event

    muted = bool(command.get("muted"))
    ts = command.get("ts")
    if ts is None:
        ts = utils.get_timestamp()

    if muted:
        # KTD3: open the interval BEFORE stopping the stream, so the row is
        # durable at command receipt and over-covers the stop latency even if a
        # later step fails — we must never stop capture leaving the over-cover
        # audio with no interval to drop it. Only when actually capturing;
        # otherwise there is no captured audio to mark, so no interval.
        # SCR-271: guard the toggle so a raise still yields a terminal event.
        # open_muted_interval (a SQLite write — "database is locked", disk-full)
        # or apply_muted (stream.stop() → PortAudioError on a device fault) can
        # raise; without this the confirming emit below is skipped and the app's
        # "Muting…" in-flight guard — cleared ONLY on a confirming/failed event —
        # strands until the recording ends. Mirror the unmute path: emit the
        # advisory audio_mute_failed. The interval opened before the stop is left
        # open BY DESIGN (KTD3 over-cover) — if the stop failed the stream is still
        # capturing, so the span is dropped as muted, the privacy-safe direction.
        try:
            if controller.capturing:
                crud.open_muted_interval(session, recording, ts)
            controller.apply_muted(True)
        except Exception as exc:  # noqa: BLE001 — a failed mute must not strand the HUD.
            logger.error(f"Audio mute toggle failed: {exc}")
            emit(EVENT_AUDIO_MUTE_FAILED)
            return EVENT_AUDIO_MUTE_FAILED
        # One-verb-one-event (KTD4): confirm EVERY mute request with exactly one
        # audio_muted event — including the idempotent no-op where the stream was
        # not capturing (an audio-on recording whose mic stream never started, or
        # a paused recording). The app arms an in-flight guard on dispatch and
        # clears it ONLY on this event, so a swallowed no-op strands the control
        # on "Muting…" until the recording ends. A not-capturing stream is
        # already effectively muted, so muted=True is truthful.
        emit(EVENT_AUDIO_MUTED, muted=True)
        return EVENT_AUDIO_MUTED

    # Unmute may (re)acquire the device, which can fail (denied / unavailable).
    try:
        event = controller.apply_muted(False)
    except Exception as exc:  # noqa: BLE001 — a denied mic must not crash audio.
        logger.error(f"Audio unmute could not acquire the mic: {exc}")
        emit(
            EVENT_AUDIO_UNMUTE_FAILED,
            reason=AUDIO_UNMUTE_FAILED_REASON_MIC_UNAVAILABLE,
        )
        return EVENT_AUDIO_UNMUTE_FAILED
    if event is not None:
        # Capture genuinely resumed: close the span (the mute axis no longer
        # suppresses audio). A no-op unmute leaves the DB untouched.
        crud.close_muted_interval(session, recording, ts)
    # One-verb-one-event (KTD4): confirm EVERY non-failed unmute request too,
    # including the idempotent no-op (capture already running, or still
    # suppressed by an independent pause) — without this the in-flight guard
    # strands on "Unmuting…" exactly as the mute path did.
    emit(EVENT_AUDIO_UNMUTED, muted=False)
    return event or EVENT_AUDIO_UNMUTED


def _make_set_muted_handler(mute_control_q):
    """Build the engine-main ``set_muted`` control-channel handler (SCR-218 U2).

    Registered in the ENGINE-MAIN process — where the control-channel reader and
    its handler registry live — NOT in the ``record_audio`` child, whose handler
    registry is a separate empty per-process global the reader would never
    consult (so registering there would silently no-op every mute). The handler
    captures the command-receipt timestamp on the engine-main clock (established
    by ``create_recording`` → ``set_start_time``) and forwards ``{muted, ts}`` to
    the audio child over ``mute_control_q``, mirroring the ``audio_rotate_q``
    fan-out. The child owns the actual stream toggle, the interval write, and the
    confirmed event (KTD4).
    """

    def _handler(command: dict) -> None:
        muted = bool(command.get("muted"))
        ts = utils.get_timestamp()
        if mute_control_q is None:
            logger.warning("set_muted received but no mute control queue; ignoring")
            return
        try:
            mute_control_q.put({"muted": muted, "ts": ts}, timeout=5)
        except Exception:  # noqa: BLE001
            logger.error("set_muted: mute control queue full or dead")

    return _handler


class _CapturePauseState:
    """Shared video/screenshot pause flag for the engine-main capture threads
    (SCR-214 U4).

    Set by the ``set_paused`` control handler; read by ``process_events`` (the
    write fan-out — the authoritative gate) and ``read_screen_events`` (skips the
    screenshot grab entirely). All three run as THREADS in the engine-main
    process, so a plain bool under a lock is GIL-safe and sufficient — no
    multiprocessing primitive is needed (the audio child, a separate process, is
    gated independently over ``pause_control_q``).

    Pause is distinct from mic-mute: while paused, capture is fully gated so the
    span records NOTHING (a genuine gap → "nothing captured", AE1), whereas a
    mic-muted span still captures video.
    """

    def __init__(self) -> None:
        self._paused = False
        self._lock = threading.Lock()

    @property
    def paused(self) -> bool:
        return self._paused

    def apply(self, paused: bool) -> bool:
        """Set the flag; return True iff it changed.

        The changed/unchanged result drives the handler's idempotency: a pause
        while already paused (or resume while running) is a no-op that neither
        forwards to the audio child nor emits a duplicate confirmed event.
        """
        paused = bool(paused)
        with self._lock:
            if self._paused == paused:
                return False
            self._paused = paused
            return True


def _apply_audio_pause_command(controller: "audio_mute.AudioStreamController",
                               command: dict) -> str | None:
    """Apply one ``set_paused`` command inside ``record_audio`` (SCR-214 U4).

    Pausing stops the mic stream (nothing captured for the span); resuming
    restarts it unless the recording is independently mic-muted. Unlike mute, no
    ``muted_intervals`` row is written — a paused span has NO frames at all
    (video is gated too), so the absence of audio is the signal; there is no
    "muted but video-captured" span to mark.

    Returns the controller's transition marker (for testability). The audio child
    does NOT emit the confirmed ``recording_paused`` / ``recording_resumed``
    event — engine-main is the sole emitter (see ``_make_set_paused_handler``). A
    failed device re-acquire on resume is swallowed and logged: the recording is
    still "resumed" (video resumed) and audio staying off is a non-fatal degraded
    state, matching the recorder's fail-open capture posture.
    """
    paused = bool(command.get("paused"))
    try:
        return controller.apply_paused(paused)
    except Exception as exc:  # noqa: BLE001 — a denied mic must not crash audio.
        logger.error(f"Audio resume could not re-acquire the mic: {exc}")
        return None


def _make_set_paused_handler(pause_state: "_CapturePauseState", pause_control_q):
    """Build the engine-main ``set_paused`` control-channel handler (SCR-214 U4).

    Registered in the ENGINE-MAIN process (where the control-channel reader +
    handler registry live), mirroring ``_make_set_muted_handler``, but extended
    beyond audio: it gates the WHOLE capture surface. On a state change it

    1. flips ``pause_state`` — the in-process gate the ``process_events`` /
       ``read_screen_events`` threads read to stop video + screenshots;
    2. forwards ``{paused, ts}`` to the audio child over ``pause_control_q`` so
       the mic stream stops/starts too;
    3. emits the confirmed ``recording_paused`` / ``recording_resumed`` event.

    Engine-main is the authoritative emitter because the video/screenshot gate in
    step 1 is deterministic (setting a flag cannot fail), unlike the audio
    re-acquire — and because a --no-audio / muted recording has no stream to
    toggle yet must still confirm pause (video stopped). A pause while already
    paused (or resume while running) is an idempotent no-op: no forward, no
    duplicate event.
    """
    from screencap._stderr_events import (
        EVENT_RECORDING_PAUSED,
        EVENT_RECORDING_RESUMED,
        emit_event,
    )

    def _handler(command: dict) -> None:
        paused = bool(command.get("paused"))
        ts = utils.get_timestamp()
        if not pause_state.apply(paused):
            return  # idempotent no-op — capture already in the requested state
        # Forward to the audio child so the mic stream is gated in parallel.
        if pause_control_q is not None:
            try:
                pause_control_q.put({"paused": paused, "ts": ts}, timeout=5)
            except Exception:  # noqa: BLE001
                logger.error("set_paused: pause control queue full or dead")
        else:
            logger.warning("set_paused received but no pause control queue")
        # Confirmed event (engine-main is the sole emitter): drives the daemon's
        # session_state["paused"], so the app reflects gated capture, not the echo.
        emit_event(
            EVENT_RECORDING_PAUSED if paused else EVENT_RECORDING_RESUMED,
            paused=paused,
        )

    return _handler


# --- Mic capture rate + software resampling -------------------------------
#
# Opening the shared default input device at a NON-NATIVE rate (the pipeline's
# 16 kHz target) makes CoreAudio collapse that device's GLOBAL buffer-frame-size
# to its minimum. That property is shared by every client of the mic, so a
# meeting app (Zoom/Meet/Teams) capturing the same mic is then driven at a
# sub-millisecond IOProc cadence and glitches the user's outbound voice —
# clearing up only once the recording stops. Capturing at the device's NATIVE
# rate keeps the shared buffer healthy; we resample to the 16 kHz on-disk /
# transcription target in software instead.


def resolve_capture_rate(target_rate: int = 16000, device: int | None = None) -> int:
    """Return the native sample rate of the input device we will actually open.

    ``device`` is the PortAudio index chosen by the Bluetooth-aware mic-source
    policy (SCR-288); when ``None`` this resolves the OS default input, preserving
    the pre-fix behaviour. Resolving for the *selected* device matters when the
    default input is a Bluetooth device we are redirecting away from — the rate
    must track the built-in mic we open, not the AirPods we skip, or the built-in
    opens at a non-native rate and reintroduces the shared-buffer collapse.

    Falls back to ``target_rate`` when the device can't be queried, preserving
    the pre-fix behaviour in that rare case rather than crashing the audio child.
    """
    import sounddevice

    try:
        if device is not None:
            info = sounddevice.query_devices(device)
        else:
            info = sounddevice.query_devices(kind="input")
        native = int(round(float(info["default_samplerate"])))
        return native or target_rate
    except Exception as exc:  # noqa: BLE001 — a query failure must not crash audio.
        logger.warning(
            f"Could not resolve input device rate ({exc}); capturing at {target_rate} Hz"
        )
        return target_rate


def resample_capture_block(
    resampler: "av.AudioResampler", frames: "np.ndarray | None", src_rate: int
) -> "np.ndarray | None":
    """Feed one native-rate float32 mono block through ``resampler``.

    ``frames`` is ``(N, 1)`` float32 (or ``None`` to flush the resampler's
    residual tail at end-of-capture). Returns the resampled ``(M, 1)`` float32
    frames ready for the 16 kHz FLAC writer, or ``None`` when the resampler
    produced no output for this block (it buffers a little internally).

    One long-lived ``resampler`` is reused for the whole recording so the
    conversion stays gapless across flush blocks AND chunk boundaries — the same
    stateful-resampler contract as ``audio_clip._decode_and_resample``.
    """
    if frames is None:
        rframes = resampler.resample(None)  # flush tail
    else:
        aframe = av.AudioFrame.from_ndarray(
            np.ascontiguousarray(frames.reshape(1, -1), dtype=np.float32),
            format="fltp",
            layout="mono",
        )
        aframe.sample_rate = src_rate
        aframe.pts = None
        rframes = resampler.resample(aframe)
    parts = [rf.to_ndarray() for rf in rframes]
    if not parts:
        return None
    return np.concatenate(parts, axis=1).reshape(-1, 1).astype(np.float32)


def record_audio(
    recording: Recording,
    db_path: str,
    terminate_processing: multiprocessing.Event,
    started_event: multiprocessing.Event,
    audio_rotate_q=None,
    audio_ack_q=None,
    mute_control_q=None,
    pause_control_q=None,
    initially_muted: bool = False,
) -> None:
    """Record audio narration during the recording and store data in database.

    Uses a streaming FLAC writer to keep RAM bounded (~2 MB) regardless of
    recording duration. A flush thread periodically drains the audio callback
    buffer and appends PCM frames to a single open SoundFile writer.

    Mid-recording mute (SCR-218 U2): the process is spawned for EVERY recording,
    but the mic device is acquired lazily — an ``initially_muted`` (``--no-audio``)
    recording constructs no ``InputStream`` (and so lights no mic-in-use
    indicator) until the first unmute. The main thread polls ``mute_control_q``
    for sub-second toggle latency; the FLAC writer is likewise opened lazily on
    the first captured frame, so a never-unmuted recording leaves no
    ``audio_*.flac`` on disk and ``has_audio`` stays false.

    Args:
        recording: The recording object.
        db_path: Path to the per-capture database file.
        terminate_processing: An event to signal the termination of the process.
        started_event: Event to set once started.
        audio_rotate_q: Chunk-rotation command queue (chunked mode only).
        audio_ack_q: Chunk-rotation ack queue (chunked mode only).
        mute_control_q: Mute/unmute command queue from the engine-main handler
            (SCR-218 U2). Each message is ``{"muted": bool, "ts": float}``.
        pause_control_q: Pause/resume command queue from the engine-main handler
            (SCR-214 U4). Each message is ``{"paused": bool, "ts": float}``.
            Pause stops the mic stream for the span (nothing captured); resume
            restarts it unless the recording is independently mic-muted.
        initially_muted: When True the recording started with audio off; no
            device is acquired until the first unmute.
    """
    utils.set_start_time(recording.timestamp)

    signal.signal(signal.SIGINT, signal.SIG_IGN)

    import sounddevice

    FLUSH_INTERVAL_SECS = 30
    SAMPLERATE = 16000
    CHANNELS = 1

    # Capture at the mic's NATIVE rate and resample to SAMPLERATE in software;
    # requesting 16 kHz from the shared device collapses its global IO buffer and
    # glitches any concurrent mic client (see resolve_capture_rate). The on-disk
    # audio_*.flac stays 16 kHz mono, so nothing downstream changes.
    CAPTURE_RATE = resolve_capture_rate(SAMPLERATE)
    _resampler = (
        av.AudioResampler(format="fltp", layout="mono", rate=SAMPLERATE)
        if CAPTURE_RATE != SAMPLERATE
        else None
    )
    logger.info(f"Audio capture rate={CAPTURE_RATE} Hz -> on-disk {SAMPLERATE} Hz")

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

    def _drain_for_write() -> np.ndarray | None:
        """Drain the callback buffer and resample it to SAMPLERATE for the FLAC.

        With native-rate capture (no resampler) this is just the drained block;
        otherwise it's the resampled 16 kHz frames (possibly ``None`` while the
        stateful resampler buffers a fraction of a window internally).

        A resample failure must not propagate: this runs on the flush thread, and
        an escaping exception would drop the ``audio_final`` ack and stall
        ``chunk_processor._wait_for_audio`` (mirrors the ack-before-close care
        below). On failure we degrade to writing nothing this cycle.
        """
        raw = _drain_buffer()
        if raw is None or _resampler is None:
            return raw
        try:
            return resample_capture_block(_resampler, raw, CAPTURE_RATE)
        except Exception as e:  # noqa: BLE001 — a bad block must not crash audio.
            logger.error(f"Audio resample failed (dropping block): {e}")
            return None

    def _flush_resampler_tail() -> np.ndarray | None:
        """Resampled residual tail to write once, at true end-of-capture."""
        if _resampler is None:
            return None
        try:
            return resample_capture_block(_resampler, None, CAPTURE_RATE)
        except Exception as e:  # noqa: BLE001 — a bad flush must not crash audio.
            logger.error(f"Audio resampler tail flush failed: {e}")
            return None

    # Track current chunk index for chunked audio
    _current_chunk_idx = [0]  # mutable container
    # Finalized flag — set by a ``final_chunk`` rotation. With the lazy writer a
    # ``None`` writer no longer uniquely means "finalized" (it also means "no
    # frames captured yet"), so the flush loop / teardown key off this instead.
    _finalized = [False]

    from pathlib import Path as _Path

    capture_dir = _Path(db_path).parent
    _is_chunked = audio_rotate_q is not None

    def _audio_path_for(idx: int):
        return capture_dir / (f"audio_{idx:04d}.flac" if _is_chunked else "audio.flac")

    def _ensure_writer(sf_writer_ref) -> None:
        """Open the current chunk's FLAC writer lazily (SCR-218 U2).

        Opening a ``SoundFile`` writes a FLAC header → a non-empty file, and
        ``has_audio`` keys on file existence — so open ONLY when there are real
        frames to write. A never-unmuted (audio-off) recording therefore leaves
        no ``audio_*.flac`` at all and stays ``has_audio=false``.
        """
        if sf_writer_ref[0] is None:
            path = _audio_path_for(_current_chunk_idx[0])
            sf_writer_ref[0] = soundfile.SoundFile(
                str(path), mode="w", samplerate=SAMPLERATE,
                channels=CHANNELS, format="FLAC",
            )

    def _rotate_audio(sf_writer_ref, capture_dir, new_idx, *, is_final=False):
        """Close the current chunk's FLAC (if any) and advance the index.

        Lazy-writer aware (SCR-218 U2): a chunk that was fully muted never
        opened a writer, so there is nothing to write/close — but the ack is
        sent REGARDLESS so ``chunk_processor._wait_for_audio`` never stalls its
        60 s budget (the always-spawn win). The next chunk's writer opens
        lazily on its first captured frame.

        When is_final=True, acks and marks finalized but does NOT advance the
        index or open a new file — there is no next chunk.
        """
        # Drain and write any remaining buffered frames for this chunk.
        frames = _drain_for_write()
        if frames is not None:
            _ensure_writer(sf_writer_ref)
            try:
                sf_writer_ref[0].write(frames)
            except Exception as e:
                logger.error(f"Audio rotation drain failed: {e}")
        if is_final:
            # Flush the resampler's residual tail into THIS (last) chunk before
            # closing — there is no next chunk to carry it.
            tail = _flush_resampler_tail()
            if tail is not None:
                _ensure_writer(sf_writer_ref)
                try:
                    sf_writer_ref[0].write(tail)
                except Exception as e:
                    logger.error(f"Audio tail flush failed: {e}")
        if sf_writer_ref[0] is not None:
            sf_writer_ref[0].close()
            sf_writer_ref[0] = None

        # Ack ALWAYS — even for a muted chunk that produced no file — so the
        # chunk processor's audio wait never stalls.
        if audio_ack_q is not None:
            try:
                audio_ack_q.put({"type": "audio_rotated", "completed_index": _current_chunk_idx[0]}, timeout=5)
            except Exception:
                logger.error("Failed to send audio rotation ack")

        if is_final:
            _finalized[0] = True  # no next chunk
            return

        _current_chunk_idx[0] = new_idx
        # Do NOT pre-open the next writer — it opens lazily on the next frame.
        logger.info(f"Audio rotated to chunk {new_idx:04d}")

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

            if _finalized[0]:
                return  # finalized by final_chunk rotation

            frames = _drain_for_write()
            if frames is not None:
                _ensure_writer(sf_writer_ref)
                try:
                    sf_writer_ref[0].write(frames)
                    logger.debug(f"Flushed {len(frames)} audio frames to disk")
                except Exception as e:
                    logger.error(f"Audio flush failed: {e}")
                    return

    # The FLAC writer is opened lazily on the first captured frame
    # (see _ensure_writer) so a never-unmuted recording leaves no file.
    sf_writer_ref = [None]  # mutable container for rotation / lazy open

    # Start the flush thread
    flush_stop = threading.Event()
    flush_thread = threading.Thread(
        target=_flush_loop,
        args=(sf_writer_ref, flush_stop, capture_dir),
        daemon=False,
    )
    flush_thread.start()

    # The mic stream is owned by AudioStreamController (SCR-218 U2): lazy device
    # acquisition means an audio-off recording constructs NOTHING until the first
    # unmute (constructing an InputStream opens the CoreAudio device + evaluates
    # mic TCC — it does not defer to .start()).
    def _make_stream():
        return sounddevice.InputStream(
            callback=audio_callback, samplerate=CAPTURE_RATE, channels=CHANNELS,
        )

    controller = audio_mute.AudioStreamController(
        _make_stream, initially_muted=bool(initially_muted),
    )
    start_timestamp = utils.get_timestamp()
    try:
        controller.start_initial()
    except Exception as e:  # noqa: BLE001 — a denied device must not crash audio.
        logger.error(f"Audio stream failed to start: {e}")
    if controller.capturing:
        logger.info("Audio recording started.")
    else:
        logger.info("Audio process started muted (lazy mic acquisition).")

    # NOTE: listener may not have actually started by now
    started_event.set()

    # Lazy DB session for muted-interval writes (opened on the first toggle).
    _mute_session_ref: list = [None]

    def _mute_session():
        if _mute_session_ref[0] is None:
            _mute_session_ref[0] = get_session_for_path(db_path)
        return _mute_session_ref[0]

    # Main thread: poll the mute + pause control queues for sub-second toggle
    # latency (Risk R-D) while staying responsive to teardown. Replaces the old
    # blocking ``terminate_processing.wait()``. Both queues carry rare, tiny
    # command lines, so a non-blocking drain + a bounded wait when idle keeps the
    # ~100 ms toggle latency without spinning.
    MUTE_POLL_SECS = 0.1
    _mute_used = False

    def _drain_control_queues() -> bool:
        """Apply all pending mute/pause commands; return True if any were seen."""
        nonlocal _mute_used
        handled = False
        if mute_control_q is not None:
            while True:
                try:
                    msg = mute_control_q.get_nowait()
                except Exception:
                    break
                _mute_used = True
                handled = True
                try:
                    _apply_audio_mute_command(
                        controller, msg, session=_mute_session(), recording=recording,
                    )
                except Exception:  # noqa: BLE001 — a bad toggle must not crash audio.
                    logger.exception("record_audio: mute command failed")
        if pause_control_q is not None:
            while True:
                try:
                    msg = pause_control_q.get_nowait()
                except Exception:
                    break
                handled = True
                try:
                    _apply_audio_pause_command(controller, msg)
                except Exception:  # noqa: BLE001 — a bad toggle must not crash audio.
                    logger.exception("record_audio: pause command failed")
        return handled

    while not terminate_processing.is_set():
        if not _drain_control_queues():
            terminate_processing.wait(timeout=MUTE_POLL_SECS)

    # Teardown: a command forwarded as we were leaving the poll loop may still be
    # sitting in a queue (the engine-main handler can enqueue right up to the
    # moment the daemon tears the engine down). Drain and apply BEFORE shutting
    # the stream, so audio between the last toggle and shutdown is recorded with
    # the right mute/pause state rather than captured unmarked (the muted-spans-
    # hold-no-audio invariant; a bare terminate check would drop the queued
    # command).
    _drain_control_queues()

    # Stop/close the stream and close any span left open by a teardown-while-
    # muted (U3). ``close_muted_interval`` is a no-op when nothing is open, so
    # only bother when a toggle was ever processed.
    controller.shutdown()
    if _mute_used:
        try:
            crud.close_muted_interval(_mute_session(), recording, utils.get_timestamp())
        except Exception:  # noqa: BLE001
            logger.exception("record_audio: closing open muted interval failed")

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
    # finalized by a final_chunk rotation above, which flushed its own tail).
    if not _finalized[0]:
        final_frames = _drain_for_write()
        if final_frames is not None:
            _ensure_writer(sf_writer_ref)
            try:
                sf_writer_ref[0].write(final_frames)
                logger.debug(f"Final flush: {len(final_frames)} audio frames")
            except Exception as e:
                logger.error(f"Final audio flush failed: {e}")
        # Flush the resampler's residual tail (no more chunks to carry it).
        tail = _flush_resampler_tail()
        if tail is not None:
            _ensure_writer(sf_writer_ref)
            try:
                sf_writer_ref[0].write(tail)
            except Exception as e:
                logger.error(f"Audio tail flush failed: {e}")

    # Send final audio ack BEFORE closing the FLAC writer. sf_writer.close()
    # can block for seconds on FLAC header finalization or raise on disk
    # errors; the chunk_processor is waiting on this ack with a 60s budget,
    # and losing it to a slow/failing close() triggers a spurious
    # "Audio ack for chunk N not received in 60s" warning even though the
    # chunk's files upload cleanly. The ack shape doesn't depend on writer
    # state — just the current chunk index.
    if audio_ack_q is not None:
        try:
            audio_ack_q.put(
                {"type": "audio_final", "completed_index": _current_chunk_idx[0]},
                timeout=5,
            )
        except Exception:
            logger.error("Failed to send audio_final ack")

    if sf_writer_ref[0] is not None:
        # Close writer — finalizes FLAC headers
        try:
            sf_writer_ref[0].close()
        except Exception as e:
            logger.error(f"sf_writer close failed: {e}")

    # Derive current audio path from chunk index (audio.flac for non-chunked).
    _final_audio_path = _audio_path_for(_current_chunk_idx[0])
    logger.info(f"Audio saved to {_final_audio_path}")

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
def _setup_network_capture(
    *,
    recording,
    capture_dir: str,
    db_path: str,
    network_config: Any,
    privacy_config: Any,
    proxy_port: int,
    terminate_processing,  # multiprocessing.Event
    num_network_events,  # multiprocessing.Value
    task_started_events: dict,
    task_by_name: dict,
    config_overrides: dict | None,
    flush_requested,
    flush_ack_counter,
    handoff_ready_event=None,
    dek: bytes | None = None,
    dek_wrapped: bytes | None = None,
    dek_nonce: bytes | None = None,
) -> dict[str, Any]:
    """Set up the V1 network capture pipeline inside :func:`record`.

    Order of operations (load-bearing):
        1. Snapshot system proxy state to ``<capture_dir>/.proxy_state.json``
           AND a durable copy under ``~/.screencap/proxy/snapshots/``.
        0.5. (V1.5 only) Persist the wrapped DEK to ``network_event_meta``
           BEFORE the proxy spawns so any encrypted body event the addon
           emits has a meta row to look up at export time. Skipped when
           ``dek_wrapped`` is None (V1 / metadata-only callers).
        2. Spawn the network writer process consuming ``network_write_q``.
        3. Spawn the proxy ``mp.Process`` (mitmproxy DumpMaster + addon)
           via ``multiprocessing.get_context("spawn")``. The plaintext
           ``dek`` is forwarded to ``run_proxy`` (V1.5); pickled across
           the spawn boundary.
        4. Wait up to 10s on ``started_event``; abort + restore on timeout.
        5. Write the global sentinel + per-recording handoff atomically.
        6. Signal ``handoff_ready_event`` so SessionController's daemon
           thread registers the proxy PID via ``pidfile.add_child``.
        7. Flip system proxy via a single ``osascript with administrator
           privileges`` call.
        8. Spawn the network reader thread (drains proxy out_q ->
           ``network_write_q`` directly; bypasses ``event_q``).

    Returns a dict capturing the state needed for teardown:
        write_q, proxy_proc, reader_thread, snapshot, sentinel_path,
        recording_dir, services_at_start.
    """
    from pathlib import Path as _Path

    from screencap.network import lifecycle as _net_lifecycle
    from screencap.network import system_proxy as _net_proxy
    from screencap.network.proxy_runner import run_proxy

    capture_dir_path = _Path(capture_dir)
    capture_dir_path.mkdir(parents=True, exist_ok=True)
    proxy_log_path = capture_dir_path / ".mitmdump.log"
    snapshot_path = capture_dir_path / ".proxy_state.json"
    durable_dir = _Path("~/.screencap/proxy/snapshots").expanduser()
    durable_dir.mkdir(parents=True, exist_ok=True)
    durable_snapshot_path = durable_dir / f"{recording.id}.proxy_state.json"
    confdir = _Path("~/.screencap/proxy").expanduser()
    worker_pid = os.getpid()
    started_at = time.time()
    worker_create_time = _net_lifecycle.proc_create_time(worker_pid)
    worker_cmdline_tail = _net_lifecycle.cmdline_tail(worker_pid)

    # Resources that need cleanup on partial-setup failure. The
    # caller (`record()`) only runs `_teardown_network_capture` when
    # `_network_state` is non-None, which only happens on full success.
    # Anything spawned, written, or flipped before the function returns
    # must be undone here on exception.
    snapshot: dict | None = None
    services_at_start: list[str] = []
    network_event_writer: multiprocessing.Process | None = None
    network_write_q: sq.SynchronizedQueue | None = None
    proxy_proc = None
    proxy_out_q = None
    sentinel_written = False
    handoff_written = False
    proxy_flipped = False
    snapshots_written = False

    def _cleanup_partial() -> None:
        # Undo in reverse order. Each step is best-effort + idempotent.
        if proxy_flipped and snapshot is not None:
            try:
                _net_proxy.restore_all(snapshot)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "_setup_network_capture cleanup: restore_all failed",
                )
        if sentinel_written:
            _net_lifecycle.delete_sentinel()
        if handoff_written:
            _net_lifecycle.delete_network_child_handoff(capture_dir_path)
        if proxy_proc is not None:
            try:
                if proxy_proc.is_alive():
                    proxy_proc.terminate()
                    proxy_proc.join(timeout=5)
                    if proxy_proc.is_alive():
                        proxy_proc.kill()
                        proxy_proc.join(timeout=2)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "_setup_network_capture cleanup: proxy_proc terminate failed",
                )
        if network_event_writer is not None:
            try:
                # The network writer listens to its own dedicated
                # terminate event (created above), not the global
                # terminate_processing. Set both — the global one for
                # other writers in case they're affected, and the
                # dedicated one to actually stop the network writer.
                terminate_processing.set()
                try:
                    network_writer_terminate.set()  # noqa: F821 — bound above
                except NameError:
                    # Cleanup ran before the dedicated event was
                    # constructed. Fall through to .terminate().
                    pass
                if network_event_writer.is_alive():
                    network_event_writer.join(timeout=5)
                    if network_event_writer.is_alive():
                        network_event_writer.terminate()
                        network_event_writer.join(timeout=2)
                task_by_name.pop("network_event_writer", None)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "_setup_network_capture cleanup: writer terminate failed",
                )
        if snapshots_written:
            try:
                snapshot_path.unlink()
            except FileNotFoundError:
                pass
            try:
                durable_snapshot_path.unlink()
            except FileNotFoundError:
                pass

    try:
        # (1) Snapshot system proxy BEFORE any mutation. Also enumerate active
        # services for the stop-time coverage-gap diff.
        services_at_start = _net_proxy.list_active_services()
        snapshot = _net_proxy.snapshot_all()
        snapshot_extra = {
            "recording_id": str(recording.id),
            "recording_dir": str(capture_dir_path),
            "snapshot_path": str(snapshot_path),
            "worker_pid": worker_pid,
            "worker_create_time": worker_create_time,
            "worker_cmdline_tail": worker_cmdline_tail,
            "started_at": started_at,
            "port": proxy_port,
        }
        _net_proxy.write_snapshot(snapshot, snapshot_path, extra=snapshot_extra)
        _net_proxy.write_snapshot(snapshot, durable_snapshot_path, extra=snapshot_extra)
        snapshots_written = True

        # (0.5) V1.5 — persist the wrapped DEK before the proxy spawns
        # so any encrypted body event the addon emits has a meta row to
        # look up at export time. Skipped when dek_wrapped is None
        # (V1 / metadata-only path; the meta row is irrelevant if no
        # body bytes are ever encrypted).
        if dek_wrapped is not None:
            session = get_session_for_path(db_path)
            try:
                crud.insert_network_event_meta(
                    session,
                    recording_id=recording.id,
                    dek_wrapped=dek_wrapped,
                    dek_nonce=dek_nonce,
                )
            finally:
                session.close()

        # (2) Network write queue + writer process.
        network_write_q = sq.SynchronizedQueue(maxsize=100)  # _META_QUEUE_SIZE
        writer_started = task_started_events.setdefault(
            "network_event_writer", multiprocessing.Event()
        )
        # Dedicated terminate event for the network writer — distinct
        # from the global ``terminate_processing`` shared by all
        # writers. The global event fires BEFORE
        # ``_teardown_network_capture`` runs, but the addon's
        # ``done()`` hook emits final ``network.tunneled`` events only
        # AFTER the proxy receives SIGTERM (which happens partway
        # through teardown). Those events flow proxy → reader →
        # ``network_write_q`` and need a live writer to drain them.
        # Keeping the writer alive until ``_teardown_network_capture``
        # sets this dedicated event (after reader_thread.join())
        # closes the silent-drop window.
        network_writer_terminate = multiprocessing.Event()
        network_event_writer = multiprocessing.Process(
            target=utils.WrapStdout(write_network_events),
            args=(
                network_write_q,
                num_network_events,
                None,  # perf_q — currently unused for network writer
                recording,
                db_path,
                network_writer_terminate,
                writer_started,
            ),
            kwargs={
                "config_overrides": config_overrides,
                "flush_requested": flush_requested,
                "flush_ack_counter": flush_ack_counter,
            },
            name="network_event_writer",
        )
        network_event_writer.start()
        task_by_name["network_event_writer"] = network_event_writer

        # (3) Spawn proxy mp.Process via spawn context.
        spawn_ctx = multiprocessing.get_context("spawn")
        proxy_out_q = spawn_ctx.Queue(maxsize=1000)
        started_event = spawn_ctx.Event()
        proxy_proc = spawn_ctx.Process(
            target=run_proxy,
            args=(
                proxy_out_q,
                recording.id,
                network_config,
                privacy_config,
                proxy_port,
                proxy_log_path,
                started_event,
                confdir,
                dek,
            ),
            name="network_proxy",
        )
        proxy_proc.start()
        proxy_pid = proxy_proc.pid

        # (4) Wait for proxy ready.
        if not started_event.wait(timeout=10.0):
            logger.error("network proxy failed to start within 10s")
            raise RuntimeError(
                "network proxy did not become ready within 10s. See "
                f"{proxy_log_path} for details."
            )

        proxy_create_time = _net_lifecycle.proc_create_time(proxy_pid)
        proxy_cmdline_tail = _net_lifecycle.cmdline_tail(proxy_pid)

        # (5) Sentinel + handoff atomic writes BEFORE flipping system proxy.
        _net_lifecycle.write_sentinel(
            worker_pid=worker_pid,
            worker_create_time=worker_create_time,
            worker_cmdline_tail=worker_cmdline_tail,
            proxy_pid=proxy_pid,
            proxy_create_time=proxy_create_time,
            proxy_cmdline_tail=proxy_cmdline_tail,
            started_at=started_at,
            port=proxy_port,
            recording_dir=str(capture_dir_path),
            snapshot_path=str(snapshot_path),
        )
        sentinel_written = True
        _net_lifecycle.write_network_child_handoff(
            capture_dir_path,
            proxy_pid=proxy_pid,
            worker_pid=worker_pid,
            started_at=started_at,
        )
        handoff_written = True

        # (6) Signal SessionController to register the proxy PID.
        if handoff_ready_event is not None:
            handoff_ready_event.set()

        # (7) Flip system proxy via single osascript admin call. The
        # except below is folded into the outer try/except so the
        # cleanup helper can also undo the proxy_flipped step.
        _net_proxy.set_proxy_all("127.0.0.1", proxy_port, services_at_start)
        proxy_flipped = True
    except BaseException:
        # Includes KeyboardInterrupt during `started_event.wait` and any
        # SystemProxyError from set_proxy_all. Cleanup undoes whatever
        # was set up before re-raising.
        terminate_processing.set()
        _cleanup_partial()
        raise

    # (8) Spawn reader thread (drains proxy out_q -> network_write_q).
    reader_started = task_started_events.setdefault(
        "network_event_reader", threading.Event()
    )
    reader_thread = threading.Thread(
        target=_network_event_reader_loop,
        args=(
            proxy_out_q,
            network_write_q,
            terminate_processing,
            reader_started,
            proxy_proc,
        ),
        name="network_event_reader",
        daemon=True,
    )
    reader_thread.start()
    task_by_name["network_event_reader"] = reader_thread

    return {
        "write_q": network_write_q,
        "proxy_proc": proxy_proc,
        "proxy_out_q": proxy_out_q,
        "reader_thread": reader_thread,
        "writer_proc": network_event_writer,
        "writer_terminate_event": network_writer_terminate,
        "snapshot": snapshot,
        "snapshot_path": snapshot_path,
        "durable_snapshot_path": durable_snapshot_path,
        "recording_dir": capture_dir_path,
        "services_at_start": services_at_start,
        "proxy_pid": proxy_pid,
    }


def _network_event_reader_loop(
    out_q,  # multiprocessing.Queue
    network_write_q: sq.SynchronizedQueue,
    terminate_event,  # multiprocessing.Event
    started_event: threading.Event,
    proxy_proc=None,  # multiprocessing.Process | None — for liveness-bound drain
) -> None:
    """Drain the proxy mp.Queue into ``network_write_q``.

    Two output paths:
    1. Regular network events -> ``network_write_q`` (writer process inserts).
       NetworkPinFailureEvent (control-only) -> ``console.print`` once per host.
    2. On ``network_write_q.put`` timeout, accumulate per-host drop counts and
       synthesize a ``NetworkDropBurstEvent(source="reader")`` once per second
       (or on terminate). The drop burst itself is put with timeout=1.0; if
       even that fails the writer is hung -> log fatal.
    """
    from screencap.engine.events import NetworkDropBurstEvent, NetworkPinFailureEvent

    started_event.set()
    seen_pin_hosts: set[str] = set()
    seen_pin_lock = threading.Lock()
    from rich.console import Console as _RichConsole

    drop_count = 0
    drop_hosts: set[str] = set()
    drop_window_start_ns: int | None = None
    _console = _RichConsole(stderr=True)

    def _flush_drop_burst() -> None:
        nonlocal drop_count, drop_hosts, drop_window_start_ns
        if drop_count <= 0 or drop_window_start_ns is None:
            return
        burst = NetworkDropBurstEvent(
            timestamp=drop_window_start_ns / 1e9,
            timestamp_ns=drop_window_start_ns,
            details_json={
                "dropped_count": drop_count,
                "hosts_affected": sorted(drop_hosts),
                "source": "reader",
            },
        )
        try:
            network_write_q.put(burst, timeout=1.0)
        except queue.Full:
            logger.fatal(
                "network_event_reader: drop_burst put timed out; writer is hung "
                "(dropped %d events across %d hosts)",
                drop_count,
                len(drop_hosts),
            )
        drop_count = 0
        drop_hosts = set()
        drop_window_start_ns = None

    def _process_event(event: object) -> None:
        nonlocal drop_count, drop_window_start_ns
        if isinstance(event, NetworkPinFailureEvent):
            host = getattr(event, "host", "?")
            with seen_pin_lock:
                if host not in seen_pin_hosts:
                    seen_pin_hosts.add(host)
                    _console.print(
                        f"[yellow]Tunneled {host} (cert pinning detected; subsequent "
                        f"traffic will pass through unobserved).[/yellow]"
                    )
            return
        try:
            network_write_q.put(event, timeout=0.05)
        except queue.Full:
            drop_count += 1
            drop_hosts.add(str(getattr(event, "host", "?")))
            if drop_window_start_ns is None:
                drop_window_start_ns = time.time_ns()

    # Main phase: drain out_q until terminate is set.
    while not terminate_event.is_set():
        try:
            event = out_q.get(timeout=0.05)
        except queue.Empty:
            # Tick the drop-burst window even when no events are flowing.
            if drop_window_start_ns is not None:
                if time.time_ns() - drop_window_start_ns >= 1_000_000_000:
                    _flush_drop_burst()
            continue
        _process_event(event)

    # Drain phase: terminate is set, but the proxy mp.Process is STILL
    # ALIVE because the engine teardown restores system proxy first
    # (osascript admin auth, can take seconds) before terminating it.
    # The addon's done() hook emits final events (notably
    # NetworkTunneledEvent — one per observed pinned host) AFTER
    # SIGTERM reaches mitmproxy, which only happens later in
    # _teardown_network_capture's step (3). A fixed 250ms empty-poll
    # timeout would exit the reader before those final events land,
    # silently dropping them.
    #
    # Correct loop: keep reading while the proxy is alive. Once the
    # OS reports the proxy exited, do a tail drain (5 empty polls,
    # ~250ms) to catch any events still in flight on the cross-process
    # mp.Queue. ``proxy_proc=None`` (test path / legacy callers) falls
    # back to the timeout-only behavior.
    _DRAIN_EMPTY_THRESHOLD = 5
    empty_polls = 0
    while True:
        try:
            event = out_q.get(timeout=0.05)
        except queue.Empty:
            empty_polls += 1
            # Exit only when the proxy has actually exited AND we've
            # observed N consecutive empty polls. The proxy_proc==None
            # path keeps the legacy timeout-only contract.
            proxy_dead = proxy_proc is None or not proxy_proc.is_alive()
            if proxy_dead and empty_polls >= _DRAIN_EMPTY_THRESHOLD:
                break
            continue
        empty_polls = 0
        _process_event(event)

    # Final flush of any pending drop-burst aggregation.
    _flush_drop_burst()


def _teardown_network_capture(state: dict[str, Any]) -> None:
    """Engine-owned teardown — single owner of proxy/system-proxy state.

    Runs IN THIS ORDER (minimizes the dead-listener window for the user's
    new connections at the cost of a longer in-flight WebSocket interruption):

    1. Restore system proxy FIRST from the start-time snapshot. The user's
       new connections route around the dying proxy as soon as this returns.
       Already-open long-lived connections (Slack WebSocket, gRPC streams)
       continue routing through mitmproxy until step 3 terminates it; they
       see a connection drop mid-stream then reconnect under the restored
       proxy config.
    2. Write `<recording_dir>/.proxy_restored` so SessionController's
       _reap_finishing_workers sees an authoritative "do nothing" signal.
    3. Terminate the proxy mp.Process (5s grace, then kill).
    4. Diff active services vs the start-time snapshot; write the
       `.network_services_changed.json` coverage-gap marker if any
       services were added/removed mid-recording (e.g. user enabled VPN).
    5. Delete the global sentinel + per-recording handoff + durable
       snapshot copy.
    """
    from screencap.network import lifecycle as _net_lifecycle
    from screencap.network import system_proxy as _net_proxy

    snapshot = state["snapshot"]
    durable_snapshot_path = state["durable_snapshot_path"]
    recording_dir = state["recording_dir"]
    services_at_start = state["services_at_start"]
    proxy_proc = state["proxy_proc"]
    reader_thread = state["reader_thread"]
    writer_proc = state.get("writer_proc")
    writer_terminate_event = state.get("writer_terminate_event")

    # (1) Restore system proxy FIRST.
    try:
        _net_proxy.restore_all(snapshot)
        logger.info("system proxy restored from snapshot")
    except Exception:  # noqa: BLE001 — restore should never block teardown
        logger.exception("system proxy restore failed during teardown")

    # (2) Marker file.
    try:
        (recording_dir / ".proxy_restored").write_text(str(time.time()))
    except OSError:
        logger.exception("failed to write .proxy_restored marker")

    # (3) Terminate proxy mp.Process. SIGTERM lands inside mitmproxy
    # which runs the addon's done() hook — that's where final
    # network.tunneled events are emitted. They flow proxy → reader
    # → network_write_q. The reader stays alive while proxy_proc.is_alive
    # (per the round-3 P1 fix); the writer stays alive because we
    # haven't set writer_terminate_event yet.
    try:
        if proxy_proc.is_alive():
            proxy_proc.terminate()
            proxy_proc.join(timeout=5)
            if proxy_proc.is_alive():
                logger.warning("proxy did not exit on SIGTERM; killing")
                proxy_proc.kill()
                proxy_proc.join(timeout=2)
    except Exception:  # noqa: BLE001
        logger.exception("error terminating proxy mp.Process")

    # Reader thread sees proxy_proc dead and finishes its drain phase.
    if reader_thread.is_alive():
        reader_thread.join(timeout=2)

    # (3.5) ONLY NOW signal the network writer to terminate. Until this
    # point the writer kept draining ``network_write_q`` so the addon's
    # done()-emitted events (which the reader just forwarded) get
    # persisted to ``recording.db.network_event``. The writer's loop
    # exits when the event is set AND the queue is empty.
    if writer_terminate_event is not None:
        writer_terminate_event.set()
    if writer_proc is not None:
        try:
            writer_proc.join(timeout=5)
            if writer_proc.is_alive():
                logger.warning(
                    "network writer did not exit in 5s after terminate; "
                    "force-terminating",
                )
                writer_proc.terminate()
                writer_proc.join(timeout=2)
        except Exception:  # noqa: BLE001
            logger.exception("error joining network writer process")

    # (4) Coverage-gap diff.
    try:
        services_at_stop = _net_proxy.list_active_services()
        marker_path = recording_dir / ".network_services_changed.json"
        _net_proxy.services_changed_marker(
            services_at_start, services_at_stop, marker_path
        )
    except Exception:  # noqa: BLE001
        logger.exception("services-changed diff failed")

    # (5) Delete sentinel + handoff + durable snapshot.
    _net_lifecycle.delete_sentinel()
    _net_lifecycle.delete_network_child_handoff(recording_dir)
    try:
        durable_snapshot_path.unlink()
    except FileNotFoundError:
        pass


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
    mute_control_q=None,
    pause_control_q=None,
    screen_filter: Any | None = None,
    # --- network proxy capture (V1) ---
    network: bool = False,
    network_handoff_ready=None,  # multiprocessing.Event | None — see comment near `terminate_processing`
    network_config: Any | None = None,
    privacy_config: Any | None = None,
    network_proxy_port: int | None = None,
    # --- network body capture (V1.5) ---
    dek: bytes | None = None,
    dek_wrapped: bytes | None = None,
    dek_nonce: bytes | None = None,
) -> None:
    """Record Screenshots/ActionEvents/WindowEvents.

    Args:
        task_description: A text description of the task to be recorded.
        terminate_processing: An event to signal the termination of the events
        processing.
        terminate_recording: An event to signal the termination of the recording.
        status_pipe: A connection to communicate recording status.
        log_memory: Whether to log memory usage.
        network: When True, spawn the mitmproxy capture pipeline.
        network_handoff_ready: SessionController-supplied mp.Event signaled
            after the proxy PID is registered in the handoff file.
        network_config: NetworkConfig instance (V1 fields only).
        privacy_config: PrivacyConfig instance (mask_domains is consumed
            by the proxy ignore_hosts regex; other fields not used at proxy layer).
        network_proxy_port: Pre-flight-negotiated port; engine flips system
            proxy to point at this port after the listener is ready.
        dek: V1.5 plaintext per-recording Data Encryption Key (32 bytes).
            ``None`` (default) for V1 callers; the addon then emits
            metadata-only events. When set, body bytes for hosts in the
            effective allowlist are AES-256-GCM-encrypted with this key.
        dek_wrapped: V1.5 KEK-wrapped DEK ciphertext (with GCM tag).
            ``None`` for V1; when set, persisted to ``network_event_meta``
            before the proxy spawns so export-time decryption can resolve
            the DEK without re-reading the KEK.
        dek_nonce: V1.5 12-byte AES-GCM nonce used to wrap ``dek``.
            ``None`` for V1.
    """
    assert config.RECORD_VIDEO or config.RECORD_IMAGES, (
        config.RECORD_VIDEO,
        config.RECORD_IMAGES,
    )

    # Build config overrides dict to propagate to spawned child processes.
    # On macOS, spawn mode re-imports modules, losing in-memory config changes.
    if recording_config is not None:
        from screencap.engine.config import build_config_overrides
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

    # Mid-recording mute (SCR-218 U2): register the ``set_muted`` control-channel
    # handler in THIS (engine-main) process — where the reader + handler registry
    # live. create_recording → set_start_time has just established this process's
    # clock, so the handler can stamp each command's receipt time before
    # forwarding to the audio child over ``mute_control_q``. Unregistered in the
    # teardown block below.
    from screencap.engine import control_channel

    control_channel.register_handler(
        "set_muted", _make_set_muted_handler(mute_control_q)
    )
    # Capture-pause (SCR-214 U4): the ``set_paused`` handler flips this in-process
    # gate — read by the ``process_events`` + ``read_screen_events`` threads to
    # stop video/screenshots — and forwards to the audio child over
    # ``pause_control_q``. Registered here (engine-main owns the reader) and
    # unregistered in the teardown block below. Distinct from mute: pause gates
    # the WHOLE capture surface, so a paused span records nothing (AE1).
    pause_state = _CapturePauseState()
    control_channel.register_handler(
        "set_paused", _make_set_paused_handler(pause_state, pause_control_q)
    )

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
                 _q.CGEventGetFlags, _q.CGEventGetIntegerValueField,
                 _q.kCGEventTapDisabledByTimeout,
                 _q.kCGEventTapDisabledByUserInput)
            _ = _ns.eventWithCGEvent_
            # Pre-resolve capture-health labeller symbols (SCR-76). The
            # supervisor's _probe_tcc_denied / _action_listener_alive may run
            # off the main thread, where pyobjc's lazy bridge resolution is
            # not thread-safe — resolve them here on the main thread first.
            _ = (_q.CGPreflightScreenCaptureAccess, _q.CGPreflightListenEventAccess)
            from ApplicationServices import (
                AXIsProcessTrustedWithOptions,
                kAXTrustedCheckOptionPrompt,
            )
            _ = (AXIsProcessTrustedWithOptions, kAXTrustedCheckOptionPrompt)
        except (ImportError, AttributeError):
            pass  # pyobjc not available; gesture capture will be skipped

    _IMAGE_QUEUE_SIZE = 20   # queues carrying PIL Images / video frames
    _META_QUEUE_SIZE = 100   # queues carrying small dicts

    event_q = queue.Queue(maxsize=_IMAGE_QUEUE_SIZE)
    screen_write_q = sq.SynchronizedQueue(maxsize=_IMAGE_QUEUE_SIZE)
    action_write_q = sq.SynchronizedQueue(maxsize=_META_QUEUE_SIZE)
    window_write_q = sq.SynchronizedQueue(maxsize=_META_QUEUE_SIZE)
    video_write_q = sq.SynchronizedQueue(maxsize=_IMAGE_QUEUE_SIZE)
    # Network capture queue + state. Lazily initialised below when network=True.
    network_write_q: sq.SynchronizedQueue | None = None
    _network_state: dict[str, Any] | None = None  # holds proxy_proc, reader_thread, snapshot, sentinel info
    # perf_q: unbounded — tiny 3-tuples (~120 bytes each), bounded perf_q
    # risks cascade deadlock (all writers block → all write queues fill)
    # Reset module-level drop counters for this recording session
    global _drop_counts
    _drop_counts = {}
    # Reset capture-health state (SCR-76) for this recording session.
    global _capture_health_counts, _listener_handles
    _capture_health_counts = {}
    _listener_handles = {}
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
            pause_state,
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
            pause_state,
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

    if network:
        # ----- Network capture setup (V1 — single-owner; engine layer) -----
        # Engine is the SINGLE OWNER of system-proxy snapshot/set/restore. The
        # top-level `recorder.start_recording()` does NOT touch system proxy
        # state -- only the engine `record()` does, here.
        try:
            _network_state = _setup_network_capture(
                recording=recording,
                capture_dir=capture_dir,
                db_path=db_path,
                network_config=network_config,
                privacy_config=privacy_config,
                proxy_port=network_proxy_port,
                terminate_processing=terminate_processing,
                num_network_events=multiprocessing.Value("i", 0),
                task_started_events=task_started_events,
                task_by_name=task_by_name,
                config_overrides=_config_overrides,
                flush_requested=flush_requested,
                flush_ack_counter=flush_ack_counter,
                handoff_ready_event=network_handoff_ready,
                dek=dek,
                dek_wrapped=dek_wrapped,
                dek_nonce=dek_nonce,
            )
            network_write_q = _network_state["write_q"]
        except Exception as exc:  # noqa: BLE001 — abort recording cleanly on setup failure
            logger.error("network capture setup failed: %s", exc)
            terminate_processing.set()
            raise

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

    # Always spawn the audio process (SCR-218 U2) — even for a ``--no-audio``
    # recording — so unmute can start capture live. The mic device is acquired
    # lazily (``initially_muted=True`` opens nothing until first unmute), and
    # always-spawning also removes today's 60 s-per-chunk ``_wait_for_audio``
    # stall that occurs with chunking on + audio off.
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
            "mute_control_q": mute_control_q,
            "pause_control_q": pause_control_q,
            "initially_muted": not config.RECORD_AUDIO,
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

    # --- Capture-health detection state (SCR-76) ---------------------------
    # macOS-only: the TCC labeller and the daemon revocation UX are macOS
    # concepts, and gating here keeps non-macOS recording behavior unchanged.
    _health_enabled = sys.platform == "darwin"
    _health_window_secs = config.CAPTURE_HEALTH_WINDOW_SECS
    _health_debounce = config.CAPTURE_HEALTH_DEBOUNCE_TICKS
    _health_prev_counts: dict | None = None
    _health_runs: dict = {}
    _health_emitted: dict = {}
    from screencap._stderr_events import emit_event as _emit_health_event

    def _task_alive(task_name: str) -> bool:
        _t = task_by_name.get(task_name)
        return bool(_t is not None and _t.is_alive())

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

            # Capture-health detection (SCR-76). Per-reader attempt-vs-output
            # over this 1s tick, debounced, attributed in-process. Detection +
            # emission ONLY — it NEVER stops the recording (fail-open). Skipped
            # during teardown to avoid the stderr pipe-drain race, and wrapped
            # so a watcher bug can't kill a healthy capture.
            if _health_enabled and not terminate_processing.is_set():
                try:
                    _hc_cur = dict(_capture_health_counts)
                    _hc_alive = {
                        "screen": _task_alive("screen_event_reader"),
                        "window": _task_alive("window_event_reader"),
                        "action": any(
                            _task_alive(_n) for _n in (
                                "keyboard_event_reader",
                                "mouse_event_reader",
                                "gesture_event_reader",
                            )
                        ),
                    }
                    for _reader, _etype in _capture_health_tick(
                        prev_counts=_health_prev_counts,
                        cur_counts=_hc_cur,
                        elapsed=time.perf_counter() - _profile_start,
                        window_secs=_health_window_secs,
                        debounce=_health_debounce,
                        runs=_health_runs,
                        emitted=_health_emitted,
                        alive=_hc_alive,
                        action_alive=_action_listener_alive(),
                        emit=_emit_health_event,
                    ):
                        logger.warning(
                            f"capture-health: '{_reader}' reader edge → {_etype}"
                        )
                    _health_prev_counts = _hc_cur
                except Exception:
                    logger.warning(
                        "capture-health check error (continuing)", exc_info=True
                    )

            time.sleep(1)
        terminate_processing.set()
    except KeyboardInterrupt:
        terminate_processing.set()

    # Stop routing mute/pause commands once the recording is winding down
    # (SCR-218 U2 mute, SCR-214 U4 pause).
    try:
        from screencap.engine import control_channel

        control_channel.unregister_handler("set_muted")
        control_channel.unregister_handler("set_paused")
    except Exception:  # noqa: BLE001
        pass

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

    # Network teardown — runs FIRST in this block (before joining other writers)
    # because the engine is the SINGLE OWNER of system-proxy state and we
    # want to restore the user's proxy config as early as possible. The
    # restore-FIRST order minimizes the dead-listener window for new
    # connections (see _teardown_network_capture docstring).
    if _network_state is not None:
        try:
            _teardown_network_capture(_network_state)
        except Exception:  # noqa: BLE001
            logger.exception("network teardown failed")

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
            "network_event_writer",
            "network_event_reader",
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
    _cleanup_qs = [screen_write_q, action_write_q, window_write_q, video_write_q, perf_q]
    if network_write_q is not None:
        _cleanup_qs.append(network_write_q)
    for q in _cleanup_qs:
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
    #         from screencap.engine import plotting
    #
    #         session = get_session_for_path(db_path)
    #         plotting.plot_performance(
    #             session, recording, save_dir=capture_dir,
    #         )
    #     except ImportError:
    #         logger.warning("matplotlib not installed, skipping performance plot")

    logger.info(f"Saved {recording_timestamp=}")

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
        # ``duration_seconds`` is the total ``record()`` wall-clock
        # (``_profile_start`` at the top of record() → here), i.e. startup +
        # capture + teardown overhead — NOT the user-perceived capture time.
        # For chunked cloud recordings the teardown can dominate, so this can
        # be much larger than the captured span. The CLI worker uses it only
        # as a non-zero ``elapsed`` fallback when its live loop never ran
        # (SCR-71).
        status_pipe.send({
            "type": "record.stopped",
            "duration_seconds": round(_profile_duration, 2),
        })


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
        video_crf: int | None = None,
        video_preset: str | None = None,
        screenshot_jpeg_quality: int | None = None,
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
        # --- Network proxy capture (V1) ---
        network: bool = False,
        network_handoff_ready=None,
        network_config: Any | None = None,
        privacy_config: Any | None = None,
        network_proxy_port: int | None = None,
        # --- Network body capture (V1.5) ---
        dek: bytes | None = None,
        dek_wrapped: bytes | None = None,
        dek_nonce: bytes | None = None,
    ) -> None:
        from pathlib import Path

        from screencap.engine.config import RecordingConfig

        self.capture_dir = str(Path(capture_dir).resolve())
        self.task_description = task_description
        self._send_profile = send_profile
        self._screen_filter = screen_filter
        self._network = network
        self._network_handoff_ready = network_handoff_ready
        self._network_config = network_config
        self._privacy_config = privacy_config
        self._network_proxy_port = network_proxy_port
        # V1.5 body-encryption material. None for V1 callers / no-network
        # recordings; set by top-level start_recording when --network is
        # active. dek plaintext is forwarded to the proxy mp.Process via
        # the spawn pickler; dek_wrapped/dek_nonce are persisted into
        # network_event_meta by _setup_network_capture.
        self._dek = dek
        self._dek_wrapped = dek_wrapped
        self._dek_nonce = dek_nonce

        # Build recording config from constructor params
        self._recording_config = RecordingConfig(
            capture_video=capture_video,
            capture_audio=capture_audio,
            capture_images=capture_images,
            capture_window_data=capture_window_data,
            capture_full_video=capture_full_video,
            video_encoding=video_encoding,
            video_pixel_format=video_pixel_format,
            video_crf=video_crf,
            video_preset=video_preset,
            screenshot_jpeg_quality=screenshot_jpeg_quality,
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
        # Engine's own recording duration (seconds), carried on the terminal
        # ``record.stopped`` status message. Used as the ``elapsed`` fallback
        # when the CLI live loop never ran a single iteration (SCR-71).
        self._recording_duration: float | None = None

        # Chunked recording queues and sync primitives
        self._chunk_rotate_q = None
        self._audio_rotate_q = None
        self._audio_ack_q = None
        self._chunk_process_q = None
        self._mute_control_q = None
        self._pause_control_q = None
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
        self._pipeline_finalized: bool = False
        self._capture = None  # lazy CaptureSession

    def _handle_status_msg(self, msg: object) -> None:
        """Apply one status message from record() to recorder state."""
        if not isinstance(msg, dict):
            return
        msg_type = msg.get("type")
        if msg_type == "record.started":
            self._ready_event.set()
        elif msg_type == "record.stopped":
            # Capture the engine's recording duration BEFORE flipping
            # ``_stopped_event`` so the value is never lost (SCR-71).
            self._recording_duration = msg.get("duration_seconds")
            self._stopped_event.set()
        elif msg_type == "record.child_died":
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

    def _drain_status_pipe(self) -> None:
        """Background thread that reads status messages from record()."""
        # Main poll loop and the post-loop final-drain are guarded
        # SEPARATELY: a transient error in the main loop must not skip the
        # final drain, or the terminal ``record.stopped`` (carrying
        # ``duration_seconds``) is lost and ``elapsed`` silently reverts to
        # 0.0 (SCR-71).
        try:
            while not self._stopped_event.is_set():
                if self._status_recv.poll(timeout=0.5):
                    self._handle_status_msg(self._status_recv.recv())
        except (EOFError, OSError):
            logger.debug("Status pipe closed during main drain loop")

        # ``finalize_pipeline`` joins the record thread (which sends the
        # terminal ``record.stopped``) and then sets ``_stopped_event``
        # directly as a backstop. That can race the loop's exit check
        # before ``record.stopped`` — carrying ``duration_seconds`` — is
        # read. Drain whatever is buffered so the duration is never lost.
        # This runs regardless of how the main loop exited.
        try:
            while self._status_recv.poll(timeout=0):
                self._handle_status_msg(self._status_recv.recv())
        except (EOFError, OSError):
            logger.debug("Status pipe closed during final drain")

        if getattr(self, "_recording_duration", None) is None:
            logger.warning(
                "Status drain finished without a terminal record.stopped; "
                "recording_duration unavailable (elapsed fallback disabled)"
            )

    def _run_record(self) -> None:
        """Thread target: apply config overrides, then call record()."""
        from screencap.engine.config import config_override

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
                mute_control_q=self._mute_control_q,
                pause_control_q=self._pause_control_q,
                screen_filter=self._screen_filter,
                network=self._network,
                network_handoff_ready=self._network_handoff_ready,
                network_config=self._network_config,
                privacy_config=self._privacy_config,
                network_proxy_port=self._network_proxy_port,
                dek=self._dek,
                dek_wrapped=self._dek_wrapped,
                dek_nonce=self._dek_nonce,
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
        # Mute-control queue (SCR-218 U2): created unconditionally (mute must
        # work regardless of chunking) and handed to the always-spawned audio
        # process; the engine-main ``set_muted`` handler feeds it.
        self._mute_control_q = multiprocessing.Queue(maxsize=100)
        # Pause-control queue (SCR-214 U4): the engine-main ``set_paused`` handler
        # forwards pause/resume to the audio child over this queue (video +
        # screenshots are gated in-process via the shared pause flag). Created
        # unconditionally, mirroring the mute queue.
        self._pause_control_q = multiprocessing.Queue(maxsize=100)
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

            # Auto-set screenshot_min_interval for chunked mode
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

    def finalize_pipeline(self) -> None:
        """Drain record/fanout/status threads. Idempotent. Does NOT close queues.

        External callers invoke this *before* draining downstream
        consumers (chunk_processor / scrub_worker) so every chunk
        message — including the ``final_chunk`` rotation pushed during
        record-thread shutdown — is forwarded onto ``_chunk_process_q``
        before a poison pill is enqueued behind it. Closing the queues
        here would race the consumers; ``__exit__`` does that after
        consumers have finished.
        """
        if self._pipeline_finalized:
            return
        self._pipeline_finalized = True
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

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.finalize_pipeline()

        # Clean up multiprocessing queues to prevent feeder-thread hangs at exit.
        for q in (self._chunk_rotate_q, self._audio_rotate_q,
                  self._audio_ack_q, self._chunk_process_q, self._mute_control_q,
                  self._pause_control_q):
            if q is not None:
                try:
                    q.cancel_join_thread()
                    q.close()
                except Exception:
                    pass

        # Close pipe connections (plain file descriptors — always safe).
        for conn in (self._status_recv, self._status_send):
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def stop(self) -> None:
        """Stop recording programmatically."""
        self._terminate_processing.set()

    def wait_for_ready(self, timeout: float = 60) -> bool:
        """Block until all recording threads/processes have started.

        Returns ``True`` once the engine reports ready, or ``False`` if the
        timeout expires **or** a stop is requested first. Honouring the stop
        signal matters when SIGTERM arrives during startup: ``stop()`` fires
        before ``record.started`` is emitted, so a bare ``_ready_event.wait``
        would park the caller until the engine finally readies (or the full
        timeout elapses) instead of proceeding to teardown (SCR-71).
        """
        deadline = time.monotonic() + timeout
        while not self._ready_event.wait(timeout=0.1):
            if self._terminate_processing.is_set():
                return False
            if time.monotonic() >= deadline:
                return False
        return True

    @property
    def is_recording(self) -> bool:
        """Whether recording is currently active."""
        return (
            self._record_thread is not None
            and self._record_thread.is_alive()
            and not self._terminate_processing.is_set()
        )

    @property
    def recording_duration(self) -> float | None:
        """Total ``record()`` wall-clock in seconds, or ``None``.

        Populated from the terminal ``record.stopped`` status message once
        the record thread has been drained (after ``finalize_pipeline`` /
        ``__exit__``). This is the FULL ``record()`` wall-clock — startup +
        capture + teardown — not the user-perceived capture time. For
        chunked cloud recordings the teardown overhead (ChunkProcessor
        drain, DB upload) can make it much larger than the captured span.
        Used only as a non-zero ``elapsed`` fallback when the live loop
        never ran (SCR-71).
        """
        return self._recording_duration

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
                from screencap.engine.capture import CaptureSession

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
