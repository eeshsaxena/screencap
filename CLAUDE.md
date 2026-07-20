# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Screencap is a macOS CLI for screen recording. The recording engine lives at `src/screencap/engine/` as an internal sub-package. Python >= 3.10, macOS only.

A native SwiftUI app shell lives at `macos/` and wraps the bundled CLI; see "macOS SwiftUI app shell" below for details.

### Privacy subsystem packages (SCR-33)

The privacy code is split into three sibling packages forming a one-way dependency DAG (`privacy` is the leaf; both halves depend on it; neither depends on the other):

- **`src/screencap/privacy/`** — the **shared privacy-model core** (leaf): the action vocabulary (`actions.py` — `PrivacyAction`, action-sets), the context×mode policy matrix (`policy.py`), the context classifier + bundle/domain maps (`classify.py`), audit records (`reasons.py` — `AuditEntry`/`ReasonCode`), the domain index loader + its bundled `data/ut1` blocklists (`domain_loader.py` + `data/`), and the pixel/region masking primitives shared by both halves (`mask_primitives.py`). Its `__init__` deliberately re-exports nothing, so importing a small shared symbol never pulls in heavier modules.
- **`src/screencap/enforcement/`** — **capture-time enforcement** (runs on every window event during recording): `recorder_enforcement.py` (`RecorderPrivacyFilter`), `window_filter.py` (the cloud/local window-title filters; the call-graph guard `tests/test_privacy_filter_call_graph.py` pins `build_privacy_filter`'s home here), `persistence.py`, `disable_log.py`, and `scrub_worker.py` (a capture-time row-deletion sidecar — *not* a text scrubber). Carries no NLP/ML import surface.
- **`src/screencap/redaction/`** — **post-hoc detection + redaction** (runs during scrubbing): the standalone string-detection/anonymization engine (`engine.py` + `pii`/`regex`/`secrets`/`resolver`/`filters`/`entity_mapping` backends), Apple Vision OCR (`ocr.py`), policy-driven image-mask orchestration (`masking.py`), and the scrub-time DB-geometry readers (`geometry.py`). Invoked through the narrow `create_default_pipeline` / `Anonymizer` / `normalize_text` seam.

`tests/test_package_boundary_call_graph.py` and `tests/redaction/test_import_lightness.py` are the durable guards for this DAG and for the shared core staying light.

### Daemon architecture (Phase 2)

The recording engine is supervised by a background daemon. CLI live-state commands (`screencap start` / `stop` / `status`) are thin HTTP clients of the daemon's `/v0/*` API over a UNIX socket at `~/.screencap/run/api.sock`. The daemon itself runs via `screencap serve` and is normally managed by a LaunchAgent installed by `screencap setup`.

When a CLI live-state command runs on a machine with no LaunchAgent installed (the headless / F3 install case), the CLI **auto-spawns** the daemon in the background via `posix_spawn` with `--idle-shutdown=600`. The auto-spawned daemon exits cleanly after 10 minutes of no requests, no event subscribers, and no active recording — preventing cron-driven `screencap status` from leaving permanent background processes. LaunchAgent-managed daemons omit the flag and run all day.

Auto-spawn diagnostic log: `~/.screencap/run/auto-serve.log` (mode 0o600).

### Unified recording processing pipeline

Recordings flow through one **disk-first pipeline** rather than a fork at recording start. Capture writes rich chunks to disk as the source of truth; destination-agnostic stages run once per chunk; a single terminal stage converges each recording toward its destination.

- **On-disk per-chunk ledger** — `src/screencap/pipeline_state.py` (`PipelineLedger`) persists per-chunk lifecycle state in a `pipeline_chunk_state` table inside the local-only `recording.db`. It is the on-disk replacement for the old in-memory `chunk_processor._chunk_results`, re-establishing the five data-loss prevention rules on disk (closed-set seeding at rotation, tri-state upload state where `SKIPPED` ≠ `UPLOADED` ≠ `FAILED`, never-delete-without-fresh-remote-confirm, frozen `chunks_expected`, crash-safe `EVICT_PENDING` ordering). The ledger is written cross-process with `busy_timeout=10000` + `BEGIN IMMEDIATE` per transition.
- **Destination-agnostic stages** — `src/screencap/pipeline_stages.py` (`PipelineStageRunner`) runs transcribe → export events → manifest once per chunk, idempotently (ledger `STAGED` + on-disk artifact existence), with no local-vs-cloud knowledge.
- **Policy resolver (monetization seam)** — `src/screencap/pipeline_policy.py` (`resolve_policy` → frozen `ResolvedPolicy{destination, retention_policy, params}`), frozen per recording into `.recording_intent` (schema v2) at start time; `set_default_override` is the single future plan-tier attachment point. `config.get_retention_policy()` is the default; `keep_forever` is the default policy (R11).
- **Terminal stage** — `src/screencap/terminal_stage.py` (`run_terminal_stage`) is the single disk-driven, idempotent convergence point. It acquires a per-recording advisory `fcntl.flock` (`~/.screencap/run/terminal-<name>.lock`) FIRST on every entry point, reconciles the ledger against GCS, routes by the frozen policy (local → no upload; cloud/both → scrubbed/masked copy via the `CloudCopyProducer` scrub-seam adapter, upload everything except `recording.db`), and writes the completeness sentinel last, gated on the frozen `chunks_expected`.
- **Universal retention & eviction** — `src/screencap/retention.py` (`evict_recording`) is decoupled from upload; the hard floor (only `UPLOADED` cloud / `LOCAL_DONE` local chunks are candidates; cloud deletes re-confirm remote NOW) holds under every policy.
- **`recording.db` is local-only by rule** — never uploaded (`upload.list_recording_files` excludes it + sidecars + `*.scrub_failed`; `upload.assert_uploadable` hard-rejects it). Cloud structured data derives only from scrubbed exports.
- **Cloud video privacy** is governed by the `masked_video_upload` flag (default OFF). See `SECURITY.md` for the capture-time-blocking-vs-post-hoc-masking trust boundary and the prerequisites for enabling it.

### Agent-memory retrieval: content index + MCP query surface (SCR-118)

A queryable retrieval surface lets a local agent search recordings. It spans three streams through one interface and is **local-only** — nothing here is uploaded.

- **Content index** — `src/screencap/content_index.py` (`ContentIndex`) owns a global FTS5 sidecar at `~/.screencap/content_index.db`, keyed by recording **directory name** + frame `timestamp_ms`. FTS5 probe + escaped-`LIKE` fallback, idempotent per-`(recording, timestamp_ms)` delete-then-insert writes, ranked pointer-only search with an `index_state` enum, `delete_recording` / `delete_recording_interval`, hardened `0o600`/`0o700` perms (symlink guard + post-WAL chmod), WAL + `busy_timeout=10000` + per-chunk transaction + PASSIVE checkpoint.
- **Index OCR pass** — `chunk_processor._index_chunk_content` runs after `_scrub_chunk_files` (config flag `content_index_enabled` / `SCREENCAP_CONTENT_INDEX`, **default off**; requires scrub enabled). It OCRs the **local, unmasked** flat `screenshots/*.jpg`, time-scoped per chunk, **skipping every frame the policy flagged** (all of `ScrubResult.blocked_intervals` — the full `SCRUB_BLOCK_ACTIONS` set, so MASK_WINDOW/secure-field/EXCLUDE/etc. are all skipped → only `ALLOW` frames are indexed), deduped via `engine.dedup`. **Strictly fail-open** (bails on `_stop_event` + a wall-clock budget); writes via `ContentIndex.write_chunk` which **replaces the whole chunk time-range** so re-process drops stale rows. The frame-selection → dedup → OCR → `write_chunk` body itself lives in **`src/screencap/index_core.py` (`index_range`)** — the shared core the live path **and** the SCR-178 backfill both call, so the skip/index rules can't drift (it owns the `content_index_write_lock()` + the unlink-before-write re-stat barrier). `scrub_worker._purge_content_index_intervals` propagates retroactive "disable this app" deletes to the index. See `SECURITY.md` for the narrowed-R7 rationale (index = same sensitivity class as the local screenshots; only ALLOW frames; never uploaded + purged on destroy).
- **Content-index backfill (SCR-178)** — `src/screencap/backfill/` (a sibling package that **does not import `screencap.daemon`** — destination-agnostic) OCR-indexes a user's *existing* recordings into `content_index.db` so Search covers history recorded before `content_index_enabled` was on. `engine.py` (`run_backfill`) enumerates recordings/chunks, seeds a closed-set resumable ledger (`ledger.py` → local-only `~/.screencap/backfill_state.db`), and per unit re-derives the skip set (`skip_intervals.py` — over the **intact** local `recording.db`, fail-closed on deleted-row coverage gaps + null-column classification ambiguity, `PrivacyMode` per `.recording_intent`) then calls `index_core.index_range`. Marks a chunk DONE only on a full pass; budget/large-library → `PAUSED`, cancel → `CANCELLED`, per-recording errors isolated → `FAILED`; strictly fail-open. Driven by daemon verbs `/v0/backfill.start|status|cancel` (`daemon/backfill_job.py`, a Supervisor-style task; progress events are **recording-name-free**, R9) and the `screencap backfill start|status|cancel` CLI group, and offered from the Search consent flow (macOS). See `SECURITY.md` for the backfill trust boundary.
- **Daemon query verbs** — `src/screencap/daemon/app.py`: `/v0/content.search`, `/v0/transcript.search`, `/v0/timeline.query`, `/v0/frame.nearest` (read-only, validated inputs, pointer-only, class-name-only diagnostics, **not** in `_ACTIVITY_PATHS`). Per-stream coverage: timeline is `authoritative`; content/transcript are best-effort. `timeline.query` omits `browser_url` in v1. **`/v0/frame.nearest` (SCR-186)** resolves a `(recording, timestamp_ms, staleness_cap_ms)` pointer to the nearest on-disk screenshot **stem** + signed `delta_ms` (no image bytes; the agent builds the `.jpg` path itself) — a port of the Swift `FrameSelection` algorithm (`src/screencap/frame_resolve.py`) with an **ALLOW-only blocked-frame filter** (`src/screencap/frame_blocked.py`, fail-closed, reusing `backfill.skip_intervals`) so it never points at a masked frame. `transcript.search` hits are additively enriched with `timestamp_ms` (chunk_start), `timestamp_granularity: "chunk"`, and `chunk_duration_ms` so a chunk-granular transcript pointer is resolvable by `frame.nearest` with a chunk-scaled cap.
- **MCP server** — `src/screencap/mcp/` (`screencap mcp`): a thin FastMCP stdio server forwarding to the daemon over its own async UDS client, holding a `/v0/events` subscription for idle-shutdown liveness, stderr-only logging. Operator setup: `docs/mcp-client-setup.md`.

### On-disk vault: encrypted container + Lock (SCR-258)

The whole local data plane (recordings tree, `content_index.db`, backfill ledger) can live inside an app-managed **encrypted sparse bundle** at `~/.screencap/store.sparsebundle` (AES-256, APFS inside), mounted transparently at the recordings dir so every consumer reads it unchanged while mounted. `src/screencap/container.py` wraps `hdiutil` (create/attach/detach/compact) + the Keychain-backed key (split-custody shared access group; only entitled binaries mount — the app daemon + bundled CLI); `src/screencap/daemon/store_lifecycle.py` resolves store state under `mount.lock` and owns the sealed sentinel. The daemon **binds first and serves a `mounted`/`locked`/`absent`/`error` `store_state`** — a sealed or absent store is a healthy serving state, never an exit (KTD-14): mutation verbs return a typed `store_locked`/`store_absent` error, read verbs carry `store_state` on their success payload. A manual, present-user (Touch ID) **Lock** (`/v0/storage.lock`|`unlock`, `screencap storage lock`|`unlock`) seals the store — stop-then-lock at a chunk boundary, sealed across reboots, no auto-lock, unlock never auto-resumes recording; the gate is a UX gate over the same-EUID posture, **not** a cryptographic boundary. Upgraders migrate plaintext libraries in via a prompted, resumable copy-verify-then-post-cutover-delete job (`src/screencap/migration.py`, `daemon/encrypt_job.py`); "change storage location" relocates the bundle (`storage_migration.py`, U11). Gated by `container_enabled` (`config.get_container_enabled`), **default ON** (SCR-258 shipped posture — the container ships from the first recording; with no installed base pre-launch there is nothing to migrate). `SECURITY.md` (the "on-disk vault container" section) is the source of truth for the at-rest boundary, the same-EUID limits, split custody, migrated-install remanence, and the no-recovery key-loss consequence.

## Common Commands

```bash
# Install (editable dev mode)
pip install -e ".[dev]"

# Run all tests
pytest tests/

# Run a single test file or test
pytest tests/test_cli.py
pytest tests/test_catalog.py::test_list_recordings_empty

# Run with coverage
pytest tests/ -v --cov
```

Linting uses `ruff` for the engine sub-package: `ruff check src/screencap/engine/`.

## Key Patterns

- All user-facing output uses `rich.console.Console` (no bare `print()`).
- Heavy imports are deferred inside CLI command bodies to keep `screencap --help` fast.
- SQLite access in the `screencap` layer uses raw `sqlite3`, not SQLAlchemy.
- Recording dirs live at `~/.screencap/recordings/<name>/`.



## Documented Solutions

`docs/solutions/` — documented solutions to past problems (bugs, best practices, workflow patterns), organized by category with YAML frontmatter (`module`, `tags`, `problem_type`). Relevant when implementing or debugging in documented areas.

## Security

`SECURITY.md` at the repo root documents the daemon socket's trust boundary (same-EUID + filesystem permissions), in-scope and out-of-scope threats, and the SCR-64 decision rationale. It is the source of truth for threat-model questions.
