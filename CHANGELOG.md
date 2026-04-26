# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Removed
- **Legacy `capture.db` schema support:** The upstream-project `capture.db` schema (read, write, scrub, viewer, samples paths) is no longer supported. Recordings produced in that format are no longer readable; only `recording.db` (the live engine schema) is supported. `screencap list` skips directories without a `recording.db`.
- **`screencap.engine.plot_capture_performance`:** Removed alongside the legacy storage layer (`Capture`, `CaptureStorage`, `create_capture`, `load_capture`, `get_storage`). No in-tree consumer existed; visualization tooling.

## [0.18.0] - 2026-04-20

### Added
- **Failed-chunk reconciliation against GCS after stop:** The chunk processor now reconciles failed chunks against GCS state after a recording stops, recovering transient upload errors without user intervention.

### Fixed
- **Intel (x86_64) binary now installs on macOS Big Sur (11.0+) again:** Lowered the x86_64 minos target back to 11.0 (was raised to 14.0 in v0.12.2) and added hard-pinned `pyinstaller/constraints-x86_64.txt` (`numpy<2.1`, `av<14`, `onnxruntime<=1.19.2`, `fast-gliner==0.1.12`, `ctranslate2<5.0`) so the Intel build only bundles wheels with Mach-O minos ≤ 11.0. ARM64 build is unchanged. Note: on Big Sur, `fast-gliner`'s bundled ONNX Runtime may fail at first PII detection and transparently fall through to the existing spaCy fallback in `screencap.privacy` (only in auto mode; explicit `pii_engine=presidio-gliner` still hard-fails).
- **`install.sh` no longer strands failed Big Sur installs in pip-fallback mode:** Removed the "skip binary if previous install used pip" shortcut. Users who hit the broken v0.17.1 x86_64 binary now recover automatically on re-install.
- **`install.sh` now accepts pre-release version tags:** Version regex loosened from digits-only to SemVer with optional pre-release (e.g. `0.18.0-rc1`), matching the release workflow's own version format.
- **`latest.txt` promotion now waits for `verify-install`:** New `promote-latest` job gates the `latest.txt` update on successful install + smoke-test across both arches, closing the race window where users could `curl install.sh` onto a just-published but unverified release.
- **Dropped `magic-wormhole` from `[record]` extras:** It was never imported in `src/`; all usage was subprocess-based via `shutil.which("wormhole")`. Internal tooling (`engine/share.py`, `_send_profiling_via_wormhole` when `send_profile=True`, Fire `capture share` subcommand) now gracefully prints "wormhole not found" if invoked. The feature was never part of the user-facing Click CLI. Eliminates the transitive `autobahn` 25.x x86_64 wheel problem and shrinks both arch bundles.
- **Unlisted cloud recordings no longer report chunk 0 as failed:** The `_unlisted` marker is now accepted by the Cloud Function's filename regex, and the client treats marker files as non-core so a server rejection never marks the chunk as failed. Previously every unlisted recording surfaced `Server returned no URL for core file _unlisted` and `0 of 1 chunks uploaded` even though the actual media uploaded fine.
- **Post-rename upload follow-up warning names the right directory:** When a recording was renamed via the menu bar or the auto-namer, the `Run screencap upload <name>` hint shown on partial uploads used to print the original timestamp-based name, which no longer matched any on-disk directory. The hint is now emitted after the rename completes, using the final directory name.
- **Post-process worker now has a SIGALRM watchdog:** A hung post-process step no longer stalls the session; the watchdog fires and releases the worker so subsequent recordings can start.
- **Silent audio-ack and auto-name failures are now surfaced:** Previously these errors were swallowed; they now appear in logs and user-facing notifications so misbehaviour is diagnosable.
- **Menu bar shows a hollow circle when idle:** Replaces the pulsing dot that looked like an ongoing recording when no recording was in progress.
- **Setup wizard `--scan` now includes safe-source apps seen during recording:** Previously these were routed past the review step, hiding them from the user.
- **Audio final-ack sent before closing FLAC writer:** Prevents a race where the last audio chunk could be truncated on stop.
- **Partial manifest removed on generation failure:** Avoids half-written manifests that blocked subsequent uploads.

### Changed
- **Bool/int env-var parsers extracted into shared helpers:** Removes duplicated parsing logic across the `config` module.
- **Writer-flush handshake extracted into shared `_flush` helper:** Reused by the sidecar scrub worker for consistent flush semantics.

## [0.17.1] - 2026-04-13

Re-release of v0.17.0 — republished to retrigger the CI release workflow. Binary content is identical to v0.17.0; entries below mirror that release for convenience.

### Added
- **Back-to-back recordings:** `screencap start` is now a long-lived session — hit Start, Stop, Start again from the menu bar without restarting the CLI, while the previous recording transcribes and uploads quietly in the background
- **Menu bar session controls:** The status-bar menu is now a full session UI with Start Recording, Stop Recording, and Quit ScreenCap items that swap in place as the session state changes
- **Audio toggle in the menu bar:** Flip microphone capture on or off for the next recording straight from the menu bar — the choice is persisted to `config.toml` and applied to subsequent recordings in the current session without a restart
- **First-seen app prompts:** The first time a new app or website appears on screen during a recording, the menu bar now pops up a prompt asking whether to allow, mask, or exclude it — and remembers your answer for next time
- **Retroactive scrubbing:** When you toggle an app or domain to "exclude" mid-recording, a new sidecar worker reaches back into the capture DB and deletes everything already recorded for that target, not just future events
- **Scrub audit log:** Every retroactive scrub decision is written to a disable log with timestamps and source, so you can review exactly what got wiped and why
- **Override replay over cached windows:** Flipping an override now re-evaluates the privacy filter against cached window history, so earlier events flip to the correct gated state instead of leaking because they were already written
- **Cloud Run display names and categorisation:** Recordings processed in Cloud Run now carry human-readable display names, classify Windows apps, and strip PII tags from task descriptions before they land in the viewer
- **Gemini on google-genai SDK:** Cloud Run's LLM segmentation pipeline now talks to Gemini through the modern `google-genai` client instead of the legacy Vertex AI SDK — no config changes needed
- **Persist-disable for privacy decisions:** Mid-recording privacy choices can now be saved so the same app is blocked by default in future sessions, not just the current one
- **Per-session scrub worker lifecycle:** The recorder now owns scrub-worker startup, shutdown, and a shared flush lock so retroactive deletes stay consistent across chunk boundaries
- **Menu bar disable events:** Toggling an app in the menu bar now publishes a disable event on the IPC bus immediately, giving the scrub worker a wake-up signal instead of waiting for the next poll
- **Session-worker injection hooks:** `start_recording` accepts external IPC queues and skip flags so the Session Controller can run it as a subprocess while reusing the persistent menu bar
- **First-seen prompt wiring:** The `prompt_enabled` flag now flows from config through the recorder into the menu bar subprocess so the first-seen prompt can be turned off globally from one place
- **`set_audio_default()` config helper:** A new setter writes the audio preference back to `config.toml` via tomlkit, preserving comments and formatting on round-trip
- **`get_first_seen_prompt_enabled()` config reader:** A matching getter exposes the first-seen prompt flag so the recorder and menu bar agree on the current value
- **Shared `_startup` helper module:** New module consolidates multiprocessing resource-tracker silencing and queue cleanup, previously inlined in `recorder.py` and now reused by the Session Controller too

### Fixed
- **Recording duration is now accurate:** Elapsed time is measured from engine-ready (not metrics-scan start) and frozen at stop (not after post-capture cleanup), so the Duration in the summary matches what `ffprobe` reports on the actual chunk files

### Changed
- **Shared atomic config save:** `save_config_atomic` is now a shared helper used by both the setup wizard and the new audio-default setter instead of being duplicated across modules

## [0.17.0] - 2026-04-11

### Added
- **Back-to-back recordings:** `screencap start` is now a long-lived session — hit Start, Stop, Start again from the menu bar without restarting the CLI, while the previous recording transcribes and uploads quietly in the background
- **Menu bar session controls:** The status-bar menu is now a full session UI with Start Recording, Stop Recording, and Quit ScreenCap items that swap in place as the session state changes
- **Audio toggle in the menu bar:** Flip microphone capture on or off for the next recording straight from the menu bar — the choice is persisted to `config.toml` and applied to subsequent recordings in the current session without a restart
- **First-seen app prompts:** The first time a new app or website appears on screen during a recording, the menu bar now pops up a prompt asking whether to allow, mask, or exclude it — and remembers your answer for next time
- **Retroactive scrubbing:** When you toggle an app or domain to "exclude" mid-recording, a new sidecar worker reaches back into the capture DB and deletes everything already recorded for that target, not just future events
- **Scrub audit log:** Every retroactive scrub decision is written to a disable log with timestamps and source, so you can review exactly what got wiped and why
- **Override replay over cached windows:** Flipping an override now re-evaluates the privacy filter against cached window history, so earlier events flip to the correct gated state instead of leaking because they were already written
- **Cloud Run display names and categorisation:** Recordings processed in Cloud Run now carry human-readable display names, classify Windows apps, and strip PII tags from task descriptions before they land in the viewer
- **Gemini on google-genai SDK:** Cloud Run's LLM segmentation pipeline now talks to Gemini through the modern `google-genai` client instead of the legacy Vertex AI SDK — no config changes needed
- **Persist-disable for privacy decisions:** Mid-recording privacy choices can now be saved so the same app is blocked by default in future sessions, not just the current one
- **Per-session scrub worker lifecycle:** The recorder now owns scrub-worker startup, shutdown, and a shared flush lock so retroactive deletes stay consistent across chunk boundaries
- **Menu bar disable events:** Toggling an app in the menu bar now publishes a disable event on the IPC bus immediately, giving the scrub worker a wake-up signal instead of waiting for the next poll
- **Session-worker injection hooks:** `start_recording` accepts external IPC queues and skip flags so the Session Controller can run it as a subprocess while reusing the persistent menu bar
- **First-seen prompt wiring:** The `prompt_enabled` flag now flows from config through the recorder into the menu bar subprocess so the first-seen prompt can be turned off globally from one place
- **`set_audio_default()` config helper:** A new setter writes the audio preference back to `config.toml` via tomlkit, preserving comments and formatting on round-trip
- **`get_first_seen_prompt_enabled()` config reader:** A matching getter exposes the first-seen prompt flag so the recorder and menu bar agree on the current value
- **Shared `_startup` helper module:** New module consolidates multiprocessing resource-tracker silencing and queue cleanup, previously inlined in `recorder.py` and now reused by the Session Controller too

### Fixed
- **Recording duration is now accurate:** Elapsed time is measured from engine-ready (not metrics-scan start) and frozen at stop (not after post-capture cleanup), so the Duration in the summary matches what `ffprobe` reports on the actual chunk files

### Changed
- **Shared atomic config save:** `save_config_atomic` is now a shared helper used by both the setup wizard and the new audio-default setter instead of being duplicated across modules

## [0.16.0] - 2026-04-09

### Added
- **exporter:** Add override loading in export paths and app list tests
- **menubar:** Add real-time app/tab list with colored marks and toggles
- **privacy:** Add runtime override support to RecorderPrivacyFilter
- **recorder:** Add IPC queues for menu bar app list and override flow
- **privacy:** Add shared override utilities and PASSWORD_MANAGER_BUNDLES
- **privacy:** Add extract_root_domain() for subdomain collapsing
- **engine:** Add app_name to window event pipeline and DB model
- **cli:** Add --set flag to settings command for changing config values
- **cloud:** Filter unlisted recordings from listings and propagate to session index
- **chunk-processor:** Upload _unlisted marker and add visibility to sentinel
- **recorder:** Propagate show_on_website through intent and sentinel
- **cli:** Add --unlisted flag and first-run visibility prompt
- **config:** Add show_on_website setting for recording visibility

### Fixed
- **engine:** Fix .pop() mutation bug and always invalidate browser URL cache

## [0.14.0] - 2026-04-02

### Added
- **cli:** Add "both" recording destination — upload to cloud AND keep a local copy
- **cli:** Print recording viewer URLs after successful upload and cloud recording
- **privacy:** Add opt-in PII/secrets scrubbing for local recordings with interactive prompt
- **privacy:** Add OCR-based PII masking for TEXT_REDACT and OCR_FALLBACK screenshots
- **privacy:** OCR pass for ALLOW screenshots using macOS Vision framework
- **llm:** Richer task descriptions and session summaries with longer field limits

### Fixed
- **window:** Use visual z-order for active window detection instead of keyboard focus
- **privacy:** Enable background window masking for all recordings (not just cloud)
- **privacy:** Force PUBLIC mode for "both" destination in scrubber
- **privacy:** Secure input (CGSIsSecureEventInputSet) blocks keystrokes only, not screenshots
- **privacy:** Allow unknown apps by default in public mode
- **privacy:** Respect z-order in mask_frame to avoid masking occluded windows
- **privacy:** Improve OCR PII detection accuracy and bounding box remapping
- **privacy:** Add Finder, Docker Desktop, Tailscale to BUNDLE_ID_MAP
- **recorder:** Prevent local file deletion for local-intent and "both" recordings
- **chunk-processor:** Simplify scrub gate to respect user opt-in

### Changed
- **setup:** Add "Both" option to setup wizard destination choices
- **config:** Accept "both" as valid upload_default value

## [0.13.1] - 2026-03-23

### Fixed
- Video PTS offset and corruption in action-gated recording mode
- Update DB video_start_time on first frame, remove num_copies workaround
- Defensive error handling in ChunkedVideoWriter chunk rotation
- Runtime invariant check and hardened T5 assertion
- Prevent silent data loss when chunk uploads are disabled
- Correct inaccurate cloud privacy notice and remove dead OCR_FALLBACK code
- Remove hf_xet from PyInstaller excludes list

## [0.13.0] - 2026-03-21

### Changed
- Migrate engine from separate `sc_engine` package to `screencap.engine` sub-package
- Merge build system into single `pyproject.toml`
- Unify scrub pipeline shared between scrubber and chunk processor

### Fixed
- Resolve migration todos — stale strings, mock method, egg-info cleanup

## [0.12.7] - 2026-03-21

### Changed
- Video/screenshot compression defaults for ~9x recording size reduction
- GLiNER switched to quantized ONNX model (634 MB → 188 MB binary)

### Fixed
- Guard gesture_callback against tap-disabled sentinel events
- Warn on export of empty recording instead of crashing
- Harden ONNX cache check and stale blob cleanup
- Fall back to pip install when arch binary unavailable in install.sh
- export_recording raises ExportError instead of returning -1
- Correct typo in setup wizard cloud option text

## [0.12.6] - 2026-03-19

### Fixed
- Gate cloud uploads on NLP model availability
- Prevent multi-chunk data loss via queue ownership proxy
- Scope setup --scan to apps seen in recordings

## [0.12.5] - 2026-03-19

### Fixed
- Use timestamp-based join in namer to resolve window titles in chunked mode
- Catch all exceptions from privacy pipeline init, not just ImportError
- Prevent silent data loss when privacy pipeline fails to initialize

### Changed
- Remove unused FK columns from ActionEvent and delete post_process_events

## [0.12.4] - 2026-03-19

### Added
- **cli:** Add list --remote and download --category commands
- **download:** Add remote session index, listing, and category filtering
- **cloud-function:** Add get-index action for session index retrieval
- **cloud-run:** Category prefixes, LLM tags, session index, expanded app list

### Fixed
- Reduce default chunk duration from 1 hour to 15 minutes
- Prevent sentinel upload after force-stop and remove stale local sentinel
- Gate sentinel upload behind all_chunks_uploaded()
- Install SIGINT/SIGTERM handlers before Recorder.__enter__()
- Preserve destination choice when setup wizard TUI is cancelled

## [0.12.3] - 2026-03-17

### Added
- **install:** Reactive pip fallback for incompatible binaries
- **ci:** Add _smoke-test CLI command and binary smoke testing workflows

### Fixed
- **ci:** Prevent presidio AnalyzerEngine from spawning subprocesses in frozen binary
- **ci:** Pre-import en_core_web_sm for frozen binary spaCy compatibility
- **ci:** Install en_core_web_sm spaCy model before PyInstaller build
- **install:** Harden install scripts from code review

## [0.12.2] - 2026-03-16

### Fixed
- **release:** Raise x86_64 minos threshold to 14.0 (numpy/X11 PyPI wheels require it)

## [0.12.1] - 2026-03-16

### Fixed
- **release:** Use per-architecture minos threshold (14.0 for arm64, 11.0 for x86_64)

## [0.12.0] - 2026-03-16

### Added
- **privacy:** Replace gliner+PyTorch with fast-gliner for ONNX NER inference

### Fixed
- **privacy:** Enable full privacy pipeline in PyInstaller binary
- **privacy:** Raise on explicit gliner request failure, consolidate fallback logic
- **privacy:** Use distribution-agnostic error messages, deduplicate spec list
- **release:** Use native Intel runner for x86_64 build, add minos verification
- **release:** Harden minos verification and skip latest.txt for pre-releases

## [0.11.0] - 2026-03-13

### Added
- **privacy:** Swap spaCy → GLiNER in Presidio, remove DataFog
- **privacy:** Add DetectionResolver, HeuristicFilter, and composable filter pipeline
- **privacy:** Add PII benchmark corpus and baseline measurement
- **privacy:** Improve benchmark with coverage, source tracking, redaction check
- **privacy:** Cross-reference element_state detections to scrub leaked keystrokes
- **privacy:** Capture per-screenshot window geometry for selective masking
- **privacy:** Split capture decisions for selective MASK_WINDOW handling
- **privacy:** Selective per-window masking at scrub time
- **privacy:** Mask chunk screenshots before cloud upload
- **privacy:** Multi-monitor coordinate mapping for selective masking
- **privacy:** Z-order-aware masking preserves foreground windows
- **privacy:** Make privacy deps install by default
- **privacy:** Download NLP models during setup wizard
- **privacy:** Prompt for model download on screencap start

### Fixed
- **privacy:** Mask sensitive background windows in video frames at capture time
- **privacy:** Force public mode for screenshot masking in cloud uploads
- **privacy:** Force public mode for cloud uploads + shared scrubbing function
- **privacy:** Don't use Rich spinner during model download
- **privacy:** Detect partial model downloads from interrupted Ctrl+C
- **privacy:** Always scrub before upload regardless of recording intent
- **privacy:** Harden masking failure paths and review findings
- **privacy:** Mask all sensitive background windows, fix geometry lookup
- **privacy:** Mask sensitive background windows during scrub
- **privacy:** Close review findings — fallback, video leak, dead code
- **privacy:** Lowercase token allowlist, remove redundant short names
- **privacy:** Reduce app/software name PERSON false positives
- **privacy:** Fix xref span replacement, harden scrub pipeline
- **privacy:** Address benchmark review findings
- **privacy:** Address code review findings from GLiNER swap

## [0.10.0] - 2026-03-11

### Added
- **privacy:** Add hybrid URL classifier with UT1 blocklist + keyword detection
- **cloud-run:** V2 manifest orchestrator and entry point routing
- **cloud-run:** Add LLM validation, fallback segmentation, and chunk mapping
- **cloud-run:** Add LLM prompt, schema, and Gemini Flash call chain
- **cloud-run:** Add event iterator and activity summary derivation
- **cloud-run:** Add app category classification maps
- **cloud-run:** Bump processor to v2, add LLM constants and defensive field access
- **cli:** Add --segmentation-mode flag to screencap start
- **recorder:** Accept segmentation_mode param in start_recording()
- **chunk_processor:** Thread segmentation_mode, skip v2 manifest scrub
- **manifest:** Split into v2 (LLM) and v1 (idle) manifest formats
- **config:** Add get_segmentation_mode() for task segmentation

### Fixed
- **privacy:** Prevent app_classes and allow_apps from bypassing URL classifier for browsers
- **privacy:** Remove parent expansion, fix shared-host and hash-routing classification

## [0.9.2] - 2026-03-10

### Added
- **privacy:** Run PII pipeline on browser_url during DB scrub
- **privacy:** Propagate browser domain through capture, export, and scrub pipelines
- **sc_engine:** AX browser URL extraction + domain on WindowSwitchEvent
- Graceful stop via SIGTERM, hard exit, and sentinel recovery in CLI
- Upload sentinel after graceful recording stop to trigger stitching
- Write local sentinel on force-exit and exception paths
- Add SIGTERM handler for graceful stop via `screencap stop`
- Add manifest retry logic for sentinel-triggered processing
- Support recording_complete.json as stitching trigger
- Add sentinel data builder and upload function
- Make GCS bucket name configurable via SCREENCAP_BUCKET env var
- Add python-dotenv dependency and auto-load .env at CLI entry

### Fixed
- Patch late-arriving browser_url onto deduplicated window events
- Preserve recording_complete.json in stub_recording
- Address code review findings from todo audit
- Downgrade normal shutdown log messages from warning to debug
- Drain and close chunk queues in screencap recorder on shutdown
- Clean up Recorder class queues on context manager exit
- Clean up multiprocessing queues after record() finishes
- Resolve import errors and constructor mismatch in exporter

## [0.9.1] - 2026-03-10

### Added
- **release:** Add changelog generation to /release command
- **ci:** Add GitHub Release creation with changelog notes to release workflow
- **recorder:** Add child process health monitoring during recording
- **setup:** Merge privacy mode and destination into single setup question

### Fixed
- **ci:** Add semver validation, deny-all permissions default, idempotent release creation; curate v0.9.0 changelog
- Replace numpy with pure-Python helper, use shared constants, harden viewer
- **viewer:** Cap events, reduce frame size, prevent multi-GB HTML files
- **recorder:** Add missing drop counters and tighten types for health monitoring
- Remove browser_events arg from test_masking and test_scrubber_policy

### Changed
- Remove dead Chrome WebSocket browser extension integration

## [0.9.0] - 2026-03-10

### Added

- **privacy:** Full privacy v3 system — policy engine, context classification, capture-time enforcement, structural masking, and post-processing filtering
- **privacy:** Interactive curses TUI setup wizard with app discovery, auto-classification heuristics, and approve-by-exception UX
- **privacy:** Per-recording cloud/local upload intent with mode forcing and upload gating
- **privacy:** PII detection engine with Presidio, DataFog/spaCy NER, regex, and secrets detectors
- **privacy:** Keystroke scrubbing pipeline for events.jsonl and chunk uploads
- **export:** Unified event export pipeline — shared foundation for CLI export and chunk processor with window.switch interleaving
- **chunking:** Background chunk processor with upload, auto-delete, and task manifest generation
- **capture:** Variable-rate capture with action-aware retention and per-action-type gating
- **capture:** Video redaction for cloud upload
- **dedup:** Screenshot deduplication via perceptual hashing
- Auto-export events.jsonl after recording stops
- Write screenshots to disk as JPEG files instead of SQLite blobs
- Disk space check before and during recording
- Guided macOS permission flow with auto-prompting and polling
- Add optional NAME argument to `screencap download`
- Cloud Run service for task-segmented session processing

### Fixed

- **privacy:** Fix PII leaks in key.shortcut events, harden fail-closed init, and enforce OCR_FALLBACK blocking
- **privacy:** Improve app discovery — exclude Python runtime, classify browsers/PWAs/updaters, reduce title false positives
- **chunking:** Harden upload pipeline with retry/recovery, drain fan-out queue on shutdown, fix final chunk loss
- **recorder:** Use os._exit in force-quit to prevent threading._shutdown deadlock
- Propagate config overrides to multiprocessing child processes

### Changed

- Rename openadapt-capture to screencap-engine/sc_engine across all imports, configs, and docs
- Remove legacy openadapt-privacy package and scrub command

## [0.8.0] - 2026-02-25

### Added

- **cli:** Add --verbose flag, refactor post-recording flow
- **recorder:** Add live UI, banner, summary, and log suppression
- **vendored:** Make log level configurable via OA_LOG_LEVEL env var

### Changed

- **cli:** Apply azure palette to CLI table headers and info labels
- **tui:** Apply azure palette to recording banner, live panel, and summary
- **html:** Update JS overlay colors and HTML layout to match azure palette
- **html:** Update component styles with glass effects and layout polish
- **html:** Apply azure color palette CSS variables and tokens

### Other

- Use get.screencap.sh for install URL and update README

## [0.7.2] - 2026-02-25

### Added

- **events:** Wrap media/function key presses into SpecialKeyEvent

### Fixed

- **install:** Remove old install dir before extraction
- **processing:** Narrow _is_special_key to only match media, function, and system keys

## [0.7.1] - 2026-02-25

### Fixed

- **updater:** Flatten nested tarball extraction to prevent PermissionError

## [0.7.0] - 2026-02-25

### Added

- **recorder:** Async AX query cache with event-aware routing
- **config,db:** Add event-aware AX depth settings and SQLite WAL mode
- **window:** Batch AX attribute reads and accept per-call max_depth

### Changed

- **recorder:** Disable auto viewer.html generation on stop
- **recorder:** Disable perf_stats_writer, memory_writer processes and plotting
- **recorder:** Disable perf_q.put() calls in all event writers

## [0.6.3] - 2026-02-24

### Fixed

- **processing:** Preserve keyboard shortcuts across interleaved mouse events

## [0.6.2] - 2026-02-24

### Added

- **transfer:** Parallelize download and upload file transfers

## [0.6.1] - 2026-02-24

### Added

- **processing:** Merge sequential keystrokes into word-level events

## [0.6.0] - 2026-02-24

### Added

- **export:** Add --downloads flag to export downloaded recordings
- **upload:** Auto-export events.jsonl during upload
- **events:** Preserve mouse move path when merging consecutive moves
- **export:** Add --all flag to export every recording
- **export:** Default output to recording dir, add --stdout flag
- **cli:** Add export command for JSONL training data output

### Fixed

- **db:** Auto-migrate missing columns for old recording schemas
- **db:** Auto-detect and migrate all missing columns from model metadata
- **db:** Migrate missing action_event columns in old recordings

### Other

- Remove schema migration changes

## [0.5.0] - 2026-02-24

### Added

- **events:** Add KeyShortcutEvent and shortcut detection pipeline
- **capture:** Optimize pipeline for 40 FPS recording
- **capture:** Switch macOS screenshots from CLI to mss with stall fallback
- Add CLI auto-update with self-replacement

### Fixed

- **capture:** Revert mss screenshot backend, keep pipeline optimizations
- **video:** Add fMP4 duration fallback and move_moov_atom guard
- Pass SCREENCAP_VERSION to sh, not curl, in install command

### Changed

- **capture:** Switch screenshot temp file from PNG to JPEG for ~2x speedup
- Remove redundant flac_data and transcribed_text from AudioInfo

## [0.4.1] - 2026-02-23

### Added

- Split dependencies into extras groups for cross-platform install

### Fixed

- Read version from installed metadata instead of hardcoded string
- Address P2 code review findings for queue backpressure
- Address P1 code review findings for queue backpressure
- Bound all recording queues to prevent unbounded memory growth

## [0.4.0] - 2026-02-23

### Added

- **download:** Add screencap download command for GCP recordings
- **video:** Switch to fragmented MP4 for crash-safe recordings
- **viewer:** Display modifier flags and scroll enrichment in HTML viewer
- **events:** Propagate modifier flags and scroll fields through data pipeline
- **recorder:** Capture modifier flags and scroll enrichment via CGEventTap

### Fixed

- Update Cloud Function URL to gen2 endpoint
- **viewer:** Handle corrupt audio files in viewer generation
- **audio:** Replace unbounded RAM accumulation with streaming FLAC writer

### Other

- Add clean-recordings and release slash commands

## [0.3.0] - 2026-02-22

### Added

- **deps:** Add faster-whisper to core dependencies
- **start:** Auto-start recording with LLM-powered naming
- **capture:** Add app_bundle_id and app_version to window events
- **metrics:** Capture running application versions at recording start
- **capture:** Capture SmartMagnify (double-tap zoom) gesture events
- **viewer:** Display pressure as variable-thickness drag overlays
- **processing:** Preserve pressure through event merging pipeline
- **capture:** Capture pressure from Force Touch trackpad and tablets
- **events:** Add pressure field to mouse event models and DB schema
- **capture:** Capture media key events (play, volume, brightness)
- **capture:** Add gesture capture (zoom/pinch/rotate) and fix drag detection

### Fixed

- **platform:** Use CGDisplayModeGetPixelWidth for correct Retina pixel ratio
- **viewer:** Store pixel_ratio during recording for correct Retina overlay positioning
- **storage:** Add MouseMagnifyEvent and MouseRotateEvent to EVENT_TYPE_MAP
- **capture:** Reset pressure immediately on mouse-up
- **capture:** Remove dead brightness VK mapping from handle_key
- **viewer:** Display key_name for raw key.down/key.up events
- **processing:** Lower drag distance threshold from 5px to 3px
- **viewer:** Replace UTF-16 surrogate pairs with BMP-safe Unicode icons

### Other

- Guard get_active_element_state against None, use config params
- Cap AX depth/timeout, add allowlist, fix window data None guard
- Move AX query off pynput callback, rate-limit in process_events
- Tune AX + FPS defaults to reduce jitter

## [0.2.0] - 2026-02-21

### Added

- Initial release of ScreenCap — macOS CLI for screen recording
- Multi-process recording engine with mouse, keyboard, and screen capture
- Time-aligned video and audio recording
- Event processing pipeline (clicks, drags, typing detection)
- Per-capture SQLite database storage
- HTML viewer for recorded sessions
- CLI commands: start, stop, list, view, info, upload, download, transcribe
- macOS permission auto-prompting (Screen Recording, Accessibility, Input Monitoring)

