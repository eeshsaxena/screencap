# Recording Engine

## What it does

Captures screen frames, mouse/keyboard input, gestures, window state, and audio into a per-recording SQLite database plus media files (chunked MP4 + FLAC). The engine is the core capture machine; everything in `engine/` is owned by it.

Wrapped as a context manager: `Recorder(config) as rec` brings up all threads and processes; exit tears them down with bounded timeouts.

## How it's wired

A hybrid: input reading is on threads inside the main process; persistence is on spawned subprocesses. A single `event_processor` thread sits between them and routes by type.

```
┌─────────────── main process ───────────────┐
│                                            │
│  reader threads                            │
│  ─ screen_event_reader  (screencapture CLI)│
│  ─ keyboard_event_reader (pynput)          │
│  ─ mouse_event_reader (pynput)             │
│  ─ window_event_reader (poll AX_QUERY_*)   │
│  ─ gesture_event_reader (CGEventTap, mac)  │
│             │                              │
│             ▼                              │
│        event_q (Queue maxsize=20)          │
│             │                              │
│             ▼                              │
│   event_processor thread                   │
│   process_events()                         │
│   ─ filters via RecorderPrivacyFilter      │
│   ─ routes by event type                   │
│             │                              │
│   ┌─────────┼──────────┬──────────┐        │
│   ▼         ▼          ▼          ▼        │
│ screen_q  video_q   action_q   window_q    │
│ (20)      (20)      (100)      (100)       │
└───┼─────────┼──────────┼──────────┼────────┘
    │         │          │          │       (multiprocessing.Queue / SynchronizedQueue)
    ▼         ▼          ▼          ▼
┌──────┐ ┌──────┐ ┌────────┐ ┌────────┐ ┌──────────┐
│screen│ │video │ │action  │ │window  │ │audio     │
│writer│ │writer│ │writer  │ │writer  │ │recorder  │
│proc  │ │proc  │ │proc    │ │proc    │ │proc      │
└──┬───┘ └──┬───┘ └────┬───┘ └────┬───┘ └────┬─────┘
   │        │          │          │           │
   ▼        ▼          ▼          ▼           ▼
recording.db    chunk_NNNN.mp4   recording.db   audio_NNNN.flac
(Screenshot)    (PyAV/ffmpeg     (ActionEvent /
+screenshots/    fragmented MP4)  WindowEvent
*.jpg
```

Internal helper threads inside `Recorder`: `_status_thread` drains a status pipe and sets `_ready_event`; `_fanout_thread` forwards chunk-rotation events to both audio and the chunk processor; `_record_thread` runs the `record()` blocking loop.

The chunk processor (`screencap/chunk_processor.py`) is a separate `threading.Thread` (not a process) that consumes rotation events from `_chunk_process_q`, queries the live DB per-chunk, writes `events_NNNN.jsonl` and `chunk_NNNN_manifest.json`, and optionally uploads. See [export-pipeline.md](./export-pipeline.md) and [segmentation.md](./segmentation.md).

## Data on disk

All under `~/.screencap/recordings/<name>/`:

| File | Owner | Notes |
|---|---|---|
| `recording.db` | engine writers | SQLAlchemy 8 tables — see [database.md](./database.md) |
| `screenshots/*.jpg` | screen writer | Only when `RECORD_IMAGES=True` |
| `chunk_NNNN.mp4` | video writer | Fragmented MP4, playable mid-recording |
| `audio_NNNN.flac` | audio recorder | One per chunk |
| `events_NNNN.jsonl` | chunk processor | Per-chunk export (cloud-intent only) |
| `chunk_NNNN_manifest.json` | chunk processor | v1 has tasks; v2 has stats only |
| `transcript_NNNN.txt/.json` | chunk processor | If audio + transcription enabled |
| `profiling.json` | engine | Event counts + drop counts + config snapshot |
| `.recording_id`, `.recording_intent`, `.recording_ready` | recorder | Sentinel + state files |
| `.menubar_overrides.json`, `.menubar_disable_log.jsonl` | menubar / scrub_worker | User decisions during recording |

## Load-bearing invariants

- **Spawn re-imports modules**. macOS uses spawn (not fork) for multiprocessing. Every writer process re-imports `screencap.engine.recorder` at startup. **Never put module-level side effects** that have observable behavior — they will run in every child. Use `apply_config_overrides()` at the start of each writer to restore the live config.
- **Writer processes ignore SIGINT.** The parent owns shutdown. Writers wait for the queue to drain or for `_terminate_processing` to fire.
- **Signal handlers installed BEFORE `Recorder.__enter__()`.** Setup itself can take seconds (spawning processes, waiting for ready). Ctrl+C during setup must be honored. The handler closures guard against `recorder=None` and use `multiprocessing.active_children()` as a fallback when `_child_pids` is empty.
- **Three-tap SIGINT semantics in the recorder layer.** 1st = graceful (set `_stop_event`, call `recorder.stop()`). 2nd = force (SIGTERM all child PIDs, sleep 1s, SIGKILL, `os._exit(1)`). 3rd+ = SIGKILL menubar + `os._exit(1)` immediately. SIGTERM (from `screencap stop`) is graceful only.
- **`_child_pids` stores raw PIDs, not Process objects.** Looking up Process objects in a signal handler can deadlock on `multiprocessing._children_lock`. Raw PIDs let `os.kill()` go through directly.
- **mss is throttled on macOS Sequoia.** Use the `screencapture` CLI for screenshots (~170ms per call). `mss.sct.grab()` can take 30s. `take_screenshot()` in `engine/utils.py` handles the platform branch. Do not call mss from the screen reader thread.
- **`SynchronizedQueue` is required on macOS.** The standard `multiprocessing.Queue.qsize()` raises `NotImplementedError` on macOS because `sem_getvalue()` is unimplemented. The wrapper in `engine/extensions/synchronized_queue.py` adds a `SharedCounter`.
- **Heavy imports deferred.** `engine/__init__.py` exposes a curated 4-name surface (`Capture`, `CaptureSession`, `create_html`, `__version__`); `Recorder` is imported function-locally in `screencap.recorder.start_recording` inside a try/except so the package is import-safe in headless environments. CLI commands import their dependencies inside the function body.
- **Action-gated video.** When `RECORD_FULL_VIDEO=False` (default), video frames are only saved when an action event fires. Idle-only periods produce no video frames. The `ScreenRetentionFilter` decides per action type.
- **Fragmented MP4 (`movflags=frag_keyframe+empty_moov+flush_packets=1`).** Means a hard kill in the middle of a chunk still produces a playable file. Removing this flag would break crash-recovery.
- **Video close runs on a separate thread with 15s max.** PyAV has a known GIL deadlock during `container.close()`. The workaround is mandatory.
- **`RECORD_AUDIO=False` default**, on by config. The audio process is the slowest to drain; its join timeout is 15s, video is 30s, others 10s.

## Before you change it

- Adding a new event type: it must be a Pydantic model (see [event-system.md](./event-system.md)), wired into `process_events()` routing, and have a writer-side handler. Don't bypass the typed event system.
- Adding a new reader thread: feed `event_q`, not a writer queue directly. The processor thread is the routing point.
- Changing chunk duration logic: rotation triggers a fan-out to audio + chunk processor. Both must ack.
- Changing config defaults: do it in `engine/config.py:Settings`, not in the CLI. The CLI only sets overrides via `RecordingConfig`.
- Adding fields to existing events: update `engine/events.py` Pydantic model, `engine/convert.py:dict_to_action_event` if it comes from DB, the writer's `event_data` dict assembly, and `engine/db/models.py` if it's persisted.

## See also

- [event-system.md](./event-system.md) — what flows through the queues
- [database.md](./database.md) — what the writers write
- [privacy.md](./privacy.md) — how `RecorderPrivacyFilter` gates capture
- [session.md](./session.md) — how `SessionController` spawns the recorder
- `CLAUDE.md` (root) — `take_screenshot()` and `get_monitor_dims()` macOS notes
