# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [0.9.0] - 2026-03-10

### Added

- **docs:** Add Export System section to CLAUDE.md and fix CLI export tests
- **scrub:** Update scrubbing pipeline for unified event format
- **chunk:** Upgrade chunk processor to use unified event pipeline
- **export:** Integrate window.switch events into CLI export pipeline
- **events:** Add shared pipeline foundation for unified event export
- Enable action-aware retention by default
- Variable-rate capture with per-action-type gating
- Video redaction for cloud upload
- **privacy:** Add upload_default step to setup wizard
- **privacy:** Gate uploads on recording intent
- **privacy:** Add per-recording cloud/local intent with mode forcing
- **privacy:** Replace setup wizard with curses TUI
- **privacy:** Add interactive group editing to setup wizard
- **privacy:** Rewrite setup wizard with approve-by-exception UX
- **privacy:** Expand known-apps DB and add allow_apps policy
- **privacy:** Add multi-layer auto-classification heuristics
- **privacy:** Add setup wizard with app discovery and config persistence
- **privacy:** Add secure input detection + capture-time keystroke blocking
- **privacy:** Add capture-time recorder enforcement for privacy v3 phase 5
- **privacy:** Add structural masking for privacy v3 phase 4
- **privacy:** Add post-processing enforcement for privacy v3 phase 3
- **privacy:** Add context association and classification for privacy v3
- **privacy:** Add policy core for privacy v3
- Add optional NAME argument to screencap download command
- Add Cloud Run service for task-segmented session processing
- Add sessions download support to cloud function and CLI
- Update catalog, viewer, namer, upload for chunked recordings
- Integrate chunking into CLI and recorder
- Add background chunk processor with upload and auto-delete
- Add task manifest generation from resting periods
- Wire chunked video/audio recording into engine
- Add chunking config to engine and screencap layers
- **recorder:** Store per-display layout and screenshot size at recording start
- **privacy:** Scrub combined keystroke sequences from events.jsonl
- **dedup:** Add screenshot deduplication via perceptual hashing
- **privacy:** Upgrade DataFog PII detector to NER via spaCy engine
- **privacy:** Add text orchestration layer and CLI scrub command
- **privacy:** Add DataFog as alternative PII engine with switchable factory
- **privacy:** Add PiiDetector (Presidio), test corpus, recall benchmark
- **privacy:** Add core detection engine with pipeline, regex, and secrets detectors
- Add Claude Code skills for capture testing and redaction guidance
- Auto-export events.jsonl after recording stops
- Write screenshots to disk as JPEG files instead of SQLite blobs
- Add disk space check before and during recording
- Step-by-step guided permission flow, one at a time
- Open System Settings and auto-navigate between permission panes
- Poll for Accessibility/Input Monitoring instead of exiting immediately
- Auto-prompt macOS permissions on screencap start

### Fixed

- **scrub:** Fix PII leaks in key.shortcut and v1 event scrubbing
- Address review findings for action-aware retention
- Skip opening new audio file on final_chunk rotation
- Drain fan-out queue on shutdown to prevent final chunk loss
- Gate stubbing on DB upload success and reject missing signed URLs
- **privacy:** Enforce cloud OCR_FALLBACK blocking despite allow_apps
- **chunk_processor:** Update all call sites to use _safe_delete
- **recorder:** Use os._exit in force-quit to prevent threading._shutdown deadlock
- **privacy:** Inline-scrub chunk uploads to prevent PII leaking to GCS
- **privacy:** Use constants in scrubber safety fallback, add chunked audio tests
- **privacy:** Consolidate shared constants and harden enforcement
- **privacy:** Harden policy invariants and fail-closed init
- **privacy:** Exclude Python runtime from app discovery entirely
- **privacy:** Classify Python, browsers, PWAs, and updaters out of unclassified
- **privacy:** Start TUI cursor on 'Needs your input' group
- **privacy:** Filter system-internal apps and hide safe group from wizard
- **privacy:** Harden fail-open paths in recorder, scrubber, and setup wizard
- **privacy:** Refine context lookup, masking, and recorder integration
- **privacy:** Address review findings — fully opaque mask + audit accuracy
- **privacy:** Address review findings — 4 privacy-critical fixes
- **privacy:** Handle image_path prefix and gate browser domain on browser window
- Harden chunk upload pipeline with retry, recovery, and safety improvements
- Require chunk files before stubbing and add verbose tip
- Redirect stderr to engine_exit.log and surface ChunkProcessor init errors
- Handle queue.Empty in ChunkProcessor and return False for empty chunks
- Log video_writer non-zero exit code after join
- **privacy:** Reduce false positives on app names in window titles
- **privacy:** Address review findings from docs/todos
- Propagate config overrides to multiprocessing child processes
- Update stale comments in cli.py and pyinstaller spec
- Remove privacy references from vendored openadapt-capture
- Remove remaining scrub references missed in initial cleanup
- Use subprocess to poll permissions (bypass macOS in-process caching)

### Changed

- **scrub:** Remove v1 backward-compat scrubbing (never released)
- Remove __slots__ from ScreenRetentionFilter
- Clean up video redaction plumbing
- **chunk_processor:** Replace _safe_trash with permanent _safe_delete
- **privacy:** Toggle icons in-place instead of moving apps between groups
- **privacy:** Align setup wizard colors with app brand palette
- **privacy:** Redesign wizard UX with inline display and per-app review
- **privacy:** Remove dead code and close PIL images properly
- **privacy:** Simplify evaluator API and trim tests to 14 meaningful cases
- Update root README, homebrew, engine config, and scripts to remove OpenAdapt references
- Remove OpenAdapt references from engine docstrings and comments
- Rename oa_recording to recording filename pattern with backward compat
- Rename OA_LOG_LEVEL to SC_LOG_LEVEL, oa.stop to sc.stop, _oa_logger to _sc_logger
- Remove chrome extension, docs, changelog, and readme from engine package
- Update docs, comments, and remove stale openadapt references
- Update test mock patches to use sc_engine module path
- Update config and build files for screencap-engine rename
- Update root screencap imports to use sc_engine
- Update internal imports from openadapt_capture to sc_engine
- Rename openadapt-capture dirs to screencap-engine/sc_engine
- Remove dead -scrubbed directory filters
- Remove openadapt-privacy package and scrub command

### Other

- Audit branch tests: delete low-value tests, merge duplicates, add coverage

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

