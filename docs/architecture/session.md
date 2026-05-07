# Session

## What it does

`SessionController` is the long-lived orchestrator that owns the menubar across multiple recordings. It spawns isolated subprocess workers per recording, coordinates a persistent menubar subprocess, and serializes post-process jobs.

Before `SessionController` existed, `screencap start` was strictly one-shot: one invocation, one recording, exit. Now the controller process persists; each `screencap start` click is a fresh recording subprocess.

## Process tree

```
┌──────────────────────────────────────────────────────────────┐
│  SessionController (parent process, persistent across runs)  │
│  ─ PYTHONWARNINGS filter installed via _startup BEFORE       │
│    multiprocessing import                                    │
│  ─ pidfile written, atexit cleanup registered                │
│  ─ control_q + menubar_event_q (persistent IPC)              │
└────┬───────────────────┬───────────────────┬─────────────────┘
     │                   │                   │
     ▼                   ▼                   ▼
┌──────────┐    ┌────────────────┐    ┌──────────────────┐
│ menubar  │    │ recording      │    │ post-process     │
│ subproc  │    │ worker subproc │    │ worker subproc   │
│          │◄──►│                │    │                  │
│ rumps /  │    │ engine.Recorder│    │ events export +  │
│ AppKit   │    │ (multi-proc    │    │ transcribe +     │
│ NSPanel  │    │  internally)   │    │ namer.auto_name  │
│ live     │    │                │    │                  │
│ across   │    │ os.setpgrp()   │    │ os.setpgrp()     │
│ recs     │    │ ignores SIGINT │    │ ignores SIGINT   │
└──────────┘    │ injects 3 IPC  │    │ SIGALRM watchdog │
                │ queues:        │    │ 900s timeout     │
                │ window_feed_q, │    │                  │
                │ override_q,    │    │                  │
                │ disable_q      │    │                  │
                └────────────────┘    └──────────────────┘
```

## Lifecycle

```
SessionController.__init__
   │ install signal handlers
   │ _spawn_menubar_persistent
   ▼
SessionController.run()
   │
   ├─ _on_start_click()  ← auto-fired immediately on run()
   │     │ allocate capture dir (suffix -2/-3 on collision)
   │     │ multiprocessing.Process(target=run_recording_worker)
   │     │ start window forwarder thread
   │     │ menubar transitions to RECORDING state
   │     │
   │     ▼
   │  state = RECORDING
   │
   ├─ menubar event loop
   │     drain _menubar_event_q with 0.5s timeout
   │     dispatch: stop_click, override, disable, quit
   │     reap _finishing_workers (10-min deadline)
   │     reap _active_postprocess
   │     start next pending postprocess (FIFO, max 1 active)
   │
   ├─ _on_stop_click()
   │     │ SIGTERM the worker (NON-BLOCKING)
   │     │ move to _finishing_workers
   │     │ state = IDLE immediately (menubar re-enables Start)
   │     │
   │     ▼
   │  worker exits → _reap_finishing_workers picks up
   │     │ enqueue post-process job
   │     │ close per-recording queues
   │
   └─ _do_shutdown() (5-min bounded)
         drain finishing workers
         drain active + pending postprocess
         force-kill laggards
         kill menubar
         close persistent queues
```

## Worker entry points

### `run_recording_worker()`

Subprocess for one recording. Calls:

1. `os.setpgrp()` — detach from controller's process group; tty SIGINT does not reach worker.
2. `signal.signal(SIGINT, SIG_IGN)` — controller owns Ctrl+C.
3. `screencap.recorder.start_recording(...)` with worker-mode policy injections (replacing the legacy `_skip_*` flags):
   - `_menubar_policy=Noop()` — controller owns the persistent menubar.
   - `_lock_policy=InheritLock()` — controller owns the pidfile.
   - `_signal_policy=NoopSignalPolicy()` — controller owns Ctrl+C.
   - `_channels=IpcChannels(window_feed, override, disable)` — three queues injected from the controller.

These keyword-only arguments thread through `start_recording` into the `RecordingPolicies` / `IpcChannels` bundles consumed by `engine.ScreenRecorder`. Standalone CLI uses the defaults (`SpawnNewMenubar` / `ClaimLock` / `ThreeTapSigint`).

Writes `.recording_ready` (JSON: `elapsed`, `completed_at`, `disk_full`) on clean exit. Writes `.recording_error.log` on exception.

### `run_postprocess_worker()`

Subprocess for one post-recording job. Calls:

1. `os.setpgrp()` + ignore SIGINT.
2. Install `SIGALRM` watchdog (`_POSTPROCESS_TIMEOUT_S=900s`, override `SCREENCAP_POSTPROCESS_TIMEOUT`).
3. Run `_postprocess_pipeline`:
   - Pick up rename file from menubar if present.
   - `_auto_export` events.jsonl.
   - If auto-naming enabled and not disk-full: `_auto_transcribe` (faster-whisper → openai-whisper → OpenAI API), then `screencap.namer.auto_name` to LLM-rename the directory.
   - `print_upload_followup`, `print_summary`, `_report_unclassified_apps`.
4. Write `.postprocess_done` on completion.

## IPC queues

### Persistent (controller ↔ menubar)

| Queue | Direction | Purpose |
|---|---|---|
| `control_q` | Controller → menubar | State transitions, window events, prompt requests |
| `menubar_event_q` | Menubar → controller | Start/Stop/Quit clicks, override decisions |

These live for the entire SessionController lifetime.

### Per-recording (controller → worker)

| Queue | Direction | Purpose |
|---|---|---|
| `window_feed_q` | Worker → menubar (forwarded) | Window switch events (live app indicator) |
| `override_q` | Menubar → worker | "Allow"/"Exclude" decisions reaching `RecorderPrivacyFilter.poll_overrides` |
| `disable_q` | Menubar → worker | Retroactive scrub triggers reaching `ScrubWorker` |

These are created fresh per recording, passed at subprocess spawn time, closed when the worker is fully reaped.

## Menubar two modes

Same `menubar.py` code, two execution modes:

- **Legacy mode** (`session_mode=False`): spawned per-recording by `recorder.py`. SIGTERMs parent PID to stop. One menubar process per recording.
- **Session mode** (`session_mode=True`): spawned once by SessionController. Multiplexed `control_q` + `menubar_event_q` carry messages across many recordings. Persists across recordings.

Mode is selected at spawn time and doesn't change. Session mode is the current default.

## Stop semantics

`_on_stop_click` is **non-blocking by design**:

1. SIGTERM the worker.
2. Move worker to `_finishing_workers` list.
3. `self._current_worker = None`.
4. State → IDLE immediately. Menubar re-enables "▶ Start Recording".

The worker's actual cleanup (joining threads, draining queues, closing DB) happens in the background. `_reap_finishing_workers` polls each loop iteration; workers that exit get their post-process job enqueued. Workers that don't exit within 10 minutes are SIGKILLed.

This means: a fresh recording can start while the previous one is still finishing. The post-process queue serializes (max 1 active job) but new recordings don't wait.

## Signal handling

Three-tap SIGINT in the controller layer:

- **Tap 1**: set `_shutdown_requested = True`. Main loop handles graceful stop.
- **Tap 2**: `_force_kill_all_children()` — SIGKILL every owned subprocess. Then `os._exit(1)`.
- **Tap 3+**: `os._exit(1)` immediately.

SIGTERM: sets `_shutdown_requested = True` only. No force-kill. Used by `screencap stop` for clean shutdown.

The recorder layer has its own three-tap SIGINT (see [recording-engine.md](./recording-engine.md)). Tty SIGINT reaches both controller and recorder by default — that's why `os.setpgrp()` in workers is mandatory.

## Auto-naming via `namer.py`

`namer.auto_name(capture_dir, local_only=False)` runs an ordered LLM provider chain after recording completes:

```
claude_cli (claude-haiku-4-5-20251001)
   │  fail / unavailable
   ▼
chatgpt_cli
   │
   ▼
anthropic_api (ANTHROPIC_API_KEY)
   │
   ▼
openai_api (gpt-4o-mini)
   │
   ▼
ollama (qwen3-vl, qwen2-vl, llava, bakllava, moondream — first available)
```

`local_only=True` skips the cloud providers. The prompt requests strict JSON: `{slug: kebab-case 3-60 chars [a-z0-9-]+, description: 2-3 sentences}`. The slug becomes the new directory name (with `-2`/`-3` collision suffix); description is written to `recording.task_description` (or `capture.task_description`).

Context assembly: up to 5 sample screenshots (resized to 1024px wide, base64 JPEG), 50 dedup'd action+window pairs, 100 distinct titles, 2000-word transcript truncation, app version list.

## Setup wizard

`screencap setup` (or first `screencap start` without `[privacy]` config) launches a curses TUI in `setup_wizard.py`. It:

- Prompts for destination (Cloud / Local / Both / Ask) → `(PrivacyMode, upload_default)`.
- In `--scan` mode: pulls `catalog.get_seen_bundle_ids()` for apps seen in past recordings but not yet classified.
- Discovers installed apps via `app_discovery.discover_installed_apps` (filesystem + Spotlight).
- Auto-classifies via `auto_classify_detailed` (7 priority layers, see [privacy.md](./privacy.md)).
- Groups into `blocked` / `communication` / `safe` / `unclassified` (plus hidden `auto_allowed` for safe-source apps).
- Saves to `[privacy]` in `~/.screencap/config.toml` via `save_config_atomic` + `invalidate_config_cache`.
- Pre-downloads GLiNER + spaCy models.

## Load-bearing invariants

- **`_startup` imported BEFORE `multiprocessing`.** `session.py` does `from screencap import _startup` (with `# noqa: F401`) at the very top of its imports. The module sets `PYTHONWARNINGS` for `multiprocessing.resource_tracker`. `resource_tracker` is spawned lazily from any multiprocessing import; it inherits env at spawn. Reordering imports breaks warning suppression.
- **Workers must `os.setpgrp()`.** Otherwise tty SIGINT reaches them and the controller's three-tap pattern doesn't work.
- **Workers ignore SIGINT.** Without `SIG_IGN`, the worker's own signal handler runs in addition to the controller's.
- **`_on_stop_click` is non-blocking.** Don't add `proc.join()`. The whole point is to let the next recording start while the previous one cleans up.
- **`.recording_ready` vs `.recording_error.log` are mutually exclusive.** One signals success, the other failure. Don't write both.
- **Post-process is serialized (max 1 active).** Two transcription jobs running concurrently would compete for CPU and audio device. The FIFO is intentional.
- **5-min shutdown deadline is bounded.** A misbehaving worker cannot hold up controller exit indefinitely.
- **Per-recording queues are fresh.** Reusing queues across recordings would carry old window events into new recording's privacy filter.
- **Menubar killed last during shutdown.** Want the user to see "Stopping..." until everything is actually stopped.
- **`_child_pids` in worker stores raw PIDs**, like the recorder layer (see [recording-engine.md](./recording-engine.md) signal-handler invariant). Same deadlock concern.
- **Auto-naming requires `local_only=False` for cloud LLMs.** `auto_name_local_only=True` in config gates them off — useful for offline/dev. Defaults to `False`.

## Before you change it

- Adding a new menubar event type: extend the message format used on `menubar_event_q`. Both menubar emit and controller dispatch need updating.
- Changing the worker subprocess injection: the `_menubar_policy` / `_lock_policy` / `_signal_policy` / `_channels` keyword args on `start_recording` are how the worker tells the engine seam to defer specific responsibilities to the controller. Add new policy axes by extending `RecordingPolicies` and adding a `Noop` implementation; don't hardcode worker-vs-CLI behavior in `start_recording`.
- Adding a new auto-named field: update `_PROMPT` in `namer.py`, the response parser, the slug regex if format changes, and `_update_task_description` for the DB write.
- Adding a new post-process step: add it to `_postprocess_pipeline` in `session.py`. Be aware of the 900s SIGALRM watchdog — long-running steps may need their own deadline.
- Changing the destination prompt in setup_wizard: the 4 destinations (Cloud/Local/Both/Ask) map to specific `(PrivacyMode, upload_default)` pairs. Adding a new destination requires updating both the matrix and downstream config consumers.

## See also

- [recording-engine.md](./recording-engine.md) — what the recording worker spawns
- [privacy.md](./privacy.md) — how `RecorderPrivacyFilter` consumes the IPC queues
- [scrubbing.md](./scrubbing.md) — `ScrubWorker` and the disable_q
- `decisions/` — why SessionController, menubar mode rationale
- `research/` — namer prompt iterations, transcribe backend selection
