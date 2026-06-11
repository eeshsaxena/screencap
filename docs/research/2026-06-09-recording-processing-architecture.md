---
date: 2026-06-09T10:35:32Z
researcher: claude
git_commit: 4b5c2802b684d5b01b20576c56a372e5876bf0ca
branch: main
repository: proteus-computer-use/screencap
topic: "Recording processing architecture — components and interactions (complete overview)"
tags: [research, codebase, pipeline, processing, chunk-processor, terminal-stage, ledger, retention, upload, scrubber, daemon, engine]
status: complete
last_updated: 2026-06-09
last_updated_by: claude
---

# Research: Recording Processing Architecture — Complete Overview

**Date**: 2026-06-09T10:35:32Z
**Git Commit**: 4b5c2802b684d5b01b20576c56a372e5876bf0ca
**Branch**: main
**Repository**: proteus-computer-use/screencap

## Research Question

How does processing currently work and what is its architecture — which components exist, and how do they interact with each other? A complete, grounded overview of the recording processing system as it exists in the codebase today.

## Summary

ScreenCap processes a recording in two connected halves, both living inside one **engine subprocess** that a background **daemon** spawns and supervises:

1. **Capture half (live, in-memory → disk):** Multiple reader threads capture screen/input/window/audio, fan into a single `event_q`, a single `event_processor` thread routes them to four type-specific `SynchronizedQueue`s, and four writer **processes** persist them to `recording.db` and to per-chunk `chunk_NNNN.mp4` / `audio_NNNN.flac` files. A `ChunkedVideoWriter` rotates chunks on a duration boundary and emits rotation notifications.

2. **Processing half (the "unified disk-first pipeline"):** Capture writes rich chunks to disk as the **source of truth**. Each completed chunk flows through destination-agnostic stages (transcribe → export events → manifest), tracked in an on-disk per-chunk **ledger** (`pipeline_chunk_state` table inside `recording.db`). A frozen **policy** (resolved once at recording start, stored in `.recording_intent`) decides the destination (local / cloud / both) and retention. A single **terminal stage** is the idempotent convergence point: it holds a per-recording advisory lock, reconciles the ledger against GCS, produces a scrubbed/masked cloud copy, uploads everything except `recording.db`, writes a completeness sentinel gated on a frozen `chunks_expected`, then runs universal retention/eviction.

The architecture is built around a set of **data-loss-prevention rules** (closed-set chunk seeding, tri-state upload status where `SKIPPED ≠ UPLOADED ≠ FAILED`, never-delete-without-fresh-remote-confirm, frozen completeness count, crash-safe `EVICT_PENDING` ordering) that are mechanically enforced on disk by the `PipelineLedger`.

There are two distinct processing drivers in the codebase today:
- **`ChunkProcessor`** — a thread inside the engine subprocess that runs the agnostic stages and (legacy) live per-chunk upload **during** recording.
- **`run_terminal_stage`** — the disk-driven convergence function intended as the single finalize point, invoked at stop, on manual `screencap upload`, and (as a wired-but-not-yet-auto-invoked seam) on daemon resume.

The whole system is currently mid-cutover: the unified pipeline modules (`pipeline_state`, `pipeline_stages`, `pipeline_policy`, `terminal_stage`, `retention`) are implemented and tested, with follow-ups tracking the live-finalize → terminal-stage cutover.

---

## High-Level Architecture

```
                         ┌─────────────────────────────────────────────┐
  screencap start ──────▶│  DAEMON (screencap serve)                    │
  (thin HTTP client)     │  • UNIX socket ~/.screencap/run/api.sock     │
  POST /v0/recording.start│ • Supervisor spawns + monitors engine        │
                         │  • EventBus (NDJSON event fan-out)           │
                         └───────────────────┬─────────────────────────┘
                                             │ spawns _engine-worker subprocess
                                             ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│  ENGINE SUBPROCESS  (run_recording_worker → start_recording → ScreenRecorder)  │
│                                                                                │
│  CAPTURE HALF (threads + writer processes)                                     │
│  ┌────────────┐  ┌──────────────┐  ┌───────────────────────────────────────┐  │
│  │ reader     │  │ event_q      │  │ event_processor thread                │  │
│  │ threads    │─▶│ (Queue, 20)  │─▶│ filter + route + fan-out              │  │
│  │ screen/kbd/│  └──────────────┘  └───────┬──────────┬──────────┬─────────┘  │
│  │ mouse/win/ │                            ▼          ▼          ▼            │
│  │ gesture    │              screen_write_q  action_write_q  window_write_q   │
│  └────────────┘              video_write_q   (SynchronizedQueue, cross-proc)  │
│                                    │          │          │                    │
│                                    ▼          ▼          ▼                    │
│              ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────┐  │
│  WRITER      │ screen  │  │ action  │  │ window  │  │ video   │  │ audio   │  │
│  PROCESSES   │ writer  │  │ writer  │  │ writer  │  │ writer  │  │ recorder│  │
│              └────┬────┘  └────┬────┘  └────┬────┘  └────┬────┘  └────┬────┘  │
│                   └────────────┴───────┬────┴────────────┘           │       │
│                                        ▼                              ▼       │
│                              recording.db (SQLite)          chunk_NNNN.mp4    │
│                                                             audio_NNNN.flac   │
│                                        │  ChunkedVideoWriter rotates ──┐      │
│                                        │  → chunk_rotate_q → fanout ───┤      │
│                                        ▼                               ▼      │
│  PROCESSING HALF                ┌──────────────────────────────────────────┐ │
│  (ChunkProcessor thread)        │ chunk_process_q  ──▶ ChunkProcessor       │ │
│                                 │  per chunk:                               │ │
│                                 │   seed ledger PENDING                     │ │
│                                 │   PipelineStageRunner.run_chunk:          │ │
│                                 │     transcribe → export events → manifest │ │
│                                 │     → ledger mark_staged                  │ │
│                                 │   (live) scrub + upload + mirror ledger   │ │
│                                 └──────────────────────────────────────────┘ │
│                                                                                │
│  ON STOP: finalize → terminal_lock → (terminal stage convergence)             │
└──────────────────────────────────────────────────────────────────────────────┘
                                             │
                                             ▼
         TERMINAL STAGE (run_terminal_stage)  — single disk-driven convergence
         fcntl.flock(terminal-<name>.lock) → read frozen policy → reconcile
         ledger vs GCS → route:
           LOCAL  → mark_local_done + retention
           CLOUD/ → CloudCopyProducer (recover → scrub → mask) → upload
           BOTH     scrubbed dir (never recording.db) → mark_uploaded
                  → write completeness sentinel (gated on chunks_expected)
                  → universal retention/eviction
```

---

## Detailed Findings

### 1. Capture Engine — Live Recording Data Flow

The capture engine lives under `src/screencap/engine/`. The core function is `record()` (`engine/recorder.py:3383`), wrapped by the `Recorder` context manager (`engine/recorder.py:4165`), orchestrated by `ScreenRecorder` / `_run_screen_recorder` (`engine/screen_recorder.py:329`).

**Reader threads** (all in the `record()` process, each emitting `Event(timestamp, type, data, extra)` namedtuples — `engine/recorder.py:91` — into a shared in-process `event_q = queue.Queue(maxsize=20)`):
- `screen_event_reader` (`engine/recorder.py:1503`) — `utils.take_screenshot()` (the `screencapture` CLI on macOS) throttled to `SCREEN_CAPTURE_FPS`, plus `get_all_window_geometries()` for video masking, emitted as the `extra` field.
- `keyboard_event_reader` (`engine/recorder.py:1935`) — pynput keyboard listener → `trigger_action_event`.
- `mouse_event_reader` (`engine/recorder.py:2051`) — pynput mouse listener (move/click/scroll).
- `gesture_event_reader` (`engine/recorder.py:2358`, macOS only) — a Quartz `CGEventTap` CFRunLoop capturing magnify/rotate/swipe/smart-magnify and harvesting pressure + modifier flags.
- `window_event_reader` (`engine/recorder.py:1621`) — polls `window.get_active_window_data()` at `AX_QUERY_INTERVAL`, value-deduplicated, enriched with browser tab URL via AX API.

**Event processor** (`engine/recorder.py:211`): one thread that drains `event_q`, applies privacy/masking dispositions, perceptual-dedups screenshots (dHash), runs AX element enrichment via an `AXQueryCache`, then **fans out** related events as an atomic group to four type-specific `SynchronizedQueue`s. The group ordering guarantees that an `action_event` row never references a screenshot that was never written — if any queue put in the group fails, the whole group is dropped (`engine/recorder.py:653`).

**`SynchronizedQueue`** (`engine/extensions/synchronized_queue.py:51`): a `multiprocessing.queues.Queue` subclass adding a `SharedCounter` so `qsize()` works cross-process.

**Writer processes** (separate OS processes spawned via `multiprocessing.Process`; each is the sole writer to its table):
- `screen_event_writer` — JPEG files in `screenshots/` + `screenshot` rows + `window_geometry` rows (`write_screen_event`, `engine/recorder.py:732`).
- `action_event_writer` — `action_event` rows (`engine/recorder.py:712`).
- `window_event_writer` — `window_event` rows (`engine/recorder.py:832`).
- `video_writer` — MP4 frames via `av`; in chunked mode uses `ChunkedVideoWriter`.
- `audio_recorder` (`engine/recorder.py:2586`) — `sounddevice` InputStream → FLAC via `soundfile`, with a flush thread and chunk rotation.
- (`network_event_writer` + a spawn-context `network_proxy` mitmproxy process when network capture is enabled.)

All writers call `apply_config_overrides()` first because macOS **spawn** mode re-imports every module in each child (`engine/recorder.py:889`). PyObjC symbols are pre-resolved on the main thread because lazy bridge resolution is not thread-safe (`engine/recorder.py:3478`).

**Action-gated video** (`RECORD_FULL_VIDEO` flag, read at `engine/recorder.py:441`, `:634`): when `True`, every screen event is re-typed `"screen/video"` and pushed to `video_write_q` (continuous video). When `False`, a video frame is only committed when an action event triggers a screen save — video frames track user input.

### 2. Chunk Rotation and Capture→Processing Handoff

`ChunkedVideoWriter.write_frame()` (`engine/video.py:771`) checks `timestamp - chunk_start_time >= chunk_duration`; on rotation it closes the current MP4, puts `{"type": "chunk_rotated", "completed_index": N-1, "chunk_start_time", "rotation_time"}` onto `chunk_rotate_q`, and opens `chunk_{N:04d}.mp4`. On close it emits `{"type": "final_chunk", ...}`.

The `_chunk_fanout` thread (`engine/recorder.py:4413`) drains `chunk_rotate_q` and forwards each message to **both** `_audio_rotate_q` (the audio process rotates its FLAC and acks on `_audio_ack_q`) and `_chunk_process_q` (the `ChunkProcessor` consumer).

**A chunk on disk** consists of: `chunk_{N:04d}.mp4`, `audio_{N:04d}.flac`, the `recording.db` rows timestamped in the chunk's span, and (produced later by processing) `events_{N:04d}.jsonl`, `transcript_{N:04d}.txt/.json`, `chunk_{N:04d}_manifest.json`.

### 3. The Unified Disk-First Pipeline — Component Map

Top-level processing modules (`src/screencap/`):

| File | Component | Role |
|---|---|---|
| `pipeline_state.py` | `PipelineLedger` | On-disk per-chunk lifecycle state (the source of truth for "what happened to each chunk") |
| `pipeline_stages.py` | `PipelineStageRunner` | Destination-agnostic transcribe → export → manifest, run once per chunk, idempotently |
| `pipeline_policy.py` | `resolve_policy` / `ResolvedPolicy` | Frozen per-recording destination + retention policy (monetization seam) |
| `chunk_processor.py` | `ChunkProcessor` | Live per-recording thread: drives stages + legacy live upload during recording |
| `terminal_stage.py` | `run_terminal_stage` / `CloudCopyProducer` | Single disk-driven convergence point (lock → reconcile → scrub/mask → upload → sentinel → retention) |
| `scrubber.py` | `Scrubber` / `scrub_recording` | PII/secrets redaction producing the `<name>-scrubbed` copy |
| `video_mask.py` | `mask_video_chunk` | Flag-gated video masking (default OFF) |
| `upload.py` | `list_recording_files`, `assert_uploadable`, `request_signed_urls`, `upload_recording` | The GCS upload seam |
| `retention.py` | `evict_recording` | Universal retention & eviction (decoupled from upload) |
| `task_manifest.py` | `generate_manifest` | Per-chunk manifest generation (v2 LLM / v1 idle) |
| `export.py` / `exporter.py` | `export_chunk_events`, `write_events_jsonl` | Event JSONL export (atomic streaming write) |

Per-chunk schema lives on the `recording` DB and is created by `engine/db/models.py` (`PipelineChunkState`, `:107`) + `engine/db/__init__._migrate_schema` (`:141`).

### 4. PipelineLedger — On-Disk Per-Chunk State (`pipeline_state.py`)

`PipelineLedger` (`pipeline_state.py:244`) is the on-disk replacement for the old in-memory `_chunk_results` dict. It wraps one `recording.db` and holds no persistent connection — each method opens a short-lived `sqlite3` connection with `PRAGMA busy_timeout=10000` and `BEGIN IMMEDIATE` per write transition (`pipeline_state.py:269`, `:381`), plus an in-process `threading.Lock` for same-process serialization.

**Schema** — `pipeline_chunk_state` table (`engine/db/models.py:107`): `id`, `recording_id` (FK CASCADE), `chunk_index`, four independent state columns (`lifecycle`, `stages_state`, `scrub_state`, `upload_state`, `evict_state`), `detail`, `updated_at`, with `UNIQUE(recording_id, chunk_index)`. The frozen `chunks_expected` count lives on the **`recording`** table (`models.py:53`), not on the per-chunk table.

**State axes** (four independent enums per row):
- `Lifecycle` (`:117`): `PENDING → STAGED → SCRUBBED → UPLOADED → EVICTED`, plus side-states `LOCAL_DONE`, `SKIPPED`, `FAILED`.
- `StageState` (`:136`): `PENDING / DONE / FAILED` (the agnostic stages).
- `ScrubState` (`:144`): `PENDING / DONE / SKIPPED / FAILED`.
- `UploadState` (`:153`): `PENDING / UPLOADED / SKIPPED / FAILED` — `UPLOADED` is the **only** state that permits eviction; `SKIPPED ≠ UPLOADED ≠ FAILED` are never conflated.
- `EvictState` (`:168`): `NONE / EVICT_PENDING / EVICTED` — `EVICT_PENDING` is committed BEFORE the unlink for crash-safety.

**The five data-loss-prevention rules** (`pipeline_state.py:13-37`), each mechanically enforced:
1. **Closed-set seeding** — `seed_chunk` (`:295`) `INSERT OR IGNORE`s a `PENDING` row at each rotation, before any processing; a mid-stage stop leaves a `PENDING` row that blocks all completeness gates.
2. **Tri-state upload** — `mark_skipped` / `mark_uploaded` / `mark_failed` write distinct `upload_state` values; gates treat each differently.
3. **Never delete without fresh remote confirm NOW** — `begin_eviction` (`:524`) re-calls `remote_exists()` at eviction time regardless of the stored `UPLOADED` value ("do not trust a historical UPLOADED", `:533`).
4. **Crash-safe EVICT_PENDING ordering** — `commit_eviction` (`:591`) runs `unlink()` while the row is still `EVICT_PENDING`, then transitions to `EVICTED` last; a crash leaves a present-but-uploaded file (safe) or a resumable `EVICT_PENDING`.
5. **Completeness from completeness evidence** — `freeze_chunks_expected` (`:326`) writes the count once and refuses conflicting refreezes; `finalize_gate_satisfied()` (`:759`) gates on the frozen count, not a live file glob, so eviction can't shrink the target.

**Key gate methods**: `all_uploaded()` (`:724`), `all_complete()` (`:740`), `finalize_gate_satisfied()` (`:759`), `chunks_in_state(...)` (`:685`), `chunks_needing_upload()` (`:715`).

**Callers**: `chunk_processor.py` (seeds + mirrors status), `pipeline_stages.py` (`mark_staged`), `terminal_stage.py` (reconcile, `mark_uploaded`, finalize gate, sentinel), `retention.py` (eviction primitives), and `reconcile_ledger_from_disk` (`pipeline_state.py:851`, the U9 migration reconciler).

### 5. PipelineStageRunner — Destination-Agnostic Stages (`pipeline_stages.py`)

`PipelineStageRunner` (`pipeline_stages.py:107`) owns ordering, idempotency, and the `STAGED` transition for the three stages every chunk passes through regardless of destination. It implements no stage itself — all three steps are **injected** by the caller (`ChunkProcessor`), which is what keeps it free of local-vs-cloud knowledge.

`run_chunk(chunk_index, start_ts, end_ts)` (`:144`):
1. **Idempotency gate** — `is_staged()` checks `stages_state == DONE`; returns `None` (no-op) if already staged.
2. **Stage 1 Transcribe** (`:171`) → `transcript_{idx:04d}.txt` + `.json`. Backends tried in order: `faster-whisper` (`WhisperModel("base", cpu, int8)`) → `openai-whisper` → OpenAI API (`whisper-1`) (`chunk_processor.py:820`). No audio → returns `None` (valid, not a failure).
3. **Stage 2 Export events** (`:176`) → `events_{idx:04d}.jsonl`. Flushes writer DB buffers, queries `action_event` + `window_event` rows in `[start_ts, end_ts)`, applies the cloud window filter, writes via atomic process-unique `.tmp` → `os.rename` (`export.py:30`, `exporter.py:173`). First line is a `_meta` header (`format_version: 2`); the last window event before the chunk is prepended with a rewritten timestamp for chunk context.
4. **Stage 3 Manifest** (`:181`) → `chunk_{idx:04d}_manifest.json`. v2 (default, LLM segmentation) writes counts + `blocked_intervals`; v1 (idle mode) does client-side task segmentation (`task_manifest.py:21`).
5. **`mark_staged`** (`:187`) is the LAST write, only after all three succeed — a crash before it leaves the chunk re-runnable.

**Two-layer idempotency**: ledger `STAGED` gate + on-disk artifact existence checks inside each step.

### 6. ChunkProcessor — Live Per-Recording Driver (`chunk_processor.py`)

`ChunkProcessor` runs as a **non-daemon thread inside the engine subprocess** (instantiated at `engine/collaborators.py:302` inside `RecordingCollaborators.start()`). It is NOT in the daemon process.

Per rotation message on `chunk_process_q`:
1. Sets `_chunk_results[idx] = PENDING`, calls `_process_chunk(msg)`.
2. Flush handshake: sets `flush_requested`, waits for all writer processes to ack on `flush_ack_counter` (shared, lock-guarded with `ScrubWorker`), so all chunk rows are committed.
3. Waits for the audio ack (`audio_rotated` for index N).
4. `_run_agnostic_stages` (`chunk_processor.py:557`): seeds the ledger `PENDING` row, builds the three step closures, runs `PipelineStageRunner.run_chunk`.
5. (Legacy live path) scrubs the chunk, uploads chunk files, mirrors `ChunkStatus` → ledger upload state (`_mirror_status_to_ledger`, `:503`).
6. At finalize: `freeze_expected_chunks` (`:531`), `checkpoint_and_upload_db`, `upload_sentinel`, `stub_recording`.

This is the component the unified-pipeline follow-ups intend to converge onto `run_terminal_stage` (see Historical Context).

### 7. Policy Resolver — The Monetization Seam (`pipeline_policy.py`)

`resolve_policy(*, destination, override=None)` (`pipeline_policy.py:142`) is called **once per recording** at start, from `engine/lock_policy.py:54` (`_write_identity_files`), and its output is immediately serialized into `.recording_intent` so no later config change can alter an in-flight recording.

- **`Destination`** (`:39`): `LOCAL` / `CLOUD` / `BOTH`. `_CLOUD_DESTINATIONS = {CLOUD, BOTH}`.
- **`RetentionPolicy`** (`:51`): `KEEP_FOREVER` (default, R11) / `DELETE_AFTER_UPLOAD` / `DELETE_AFTER_DAYS` (`params["days"]`) / `SIZE_CAP` (`params["size_cap_mb"]`).
- **`ResolvedPolicy`** (`:66`): a `frozen=True, slots=True` dataclass `{destination, retention_policy, params}`, with `requires_account` (`:87`, True for cloud/both — the R16 account gate), `to_dict`/`from_dict` for schema-v2 serialization (`from_dict` returns `None` for legacy v1 intents rather than raising).
- **`set_default_override(fn)`** (`:208`) installs a process-wide `Callable[[ResolvedPolicy], ResolvedPolicy]` hook — the single future plan-tier attachment point. It stays `None` in production today.
- The config default comes from `config.get_retention_policy()` (`config.py:250`): env `SCREENCAP_RETENTION_POLICY` → `[retention]` TOML → legacy `auto_delete_after_upload` bool → `"keep_forever"`.

### 8. Terminal Stage — The Single Convergence Point (`terminal_stage.py`)

`run_terminal_stage(recording_dir, ...)` (`terminal_stage.py:526`) is the disk-driven, idempotent finalize. Flow:

0. **Acquire `terminal_lock`** (`:565`) — a per-recording two-layer lock: an in-process `threading.Lock` (`:177`) FIRST, then `fcntl.flock(LOCK_EX|LOCK_NB)` on `~/.screencap/run/terminal-<name>.lock` (`:263`). Modes: `non_blocking=True` raises `TerminalStageBusy` immediately on contention; default blocks-with-timeout (600s). Degrades gracefully (in-process lock only) on NFS / unsupported flock.
1. **Read frozen policy** (`:590`) via `catalog.read_intent_policy`; fallback `LOCAL`.
2. **Open ledger** (`:594`); `result.n_expected = ledger.chunks_expected()`.
3a. **LOCAL route** (`:598`): `_route_local` marks every chunk `LOCAL_DONE`, then `_apply_retention`. Returns early — no scrub, no upload.
3b. **CLOUD/BOTH route** — `_route_cloud` (`:728`):
   - **Reconcile ledger vs GCS** (`:757`): `_reconcile_ledger_against_gcs` (`:879`) flips `PENDING`/`FAILED` rows to `UPLOADED` for chunks confirmed present in GCS (via `_chunk_confirmed_remote`, `:918`, which probes through `request_signed_urls` and treats a `url=None` response as "already there").
   - **Produce cloud copy** (`:763`): `CloudCopyProducer(recording_dir).produce(ledger, force)` (`:329`) runs recover → scrub → mask (see §9). Fail-closed: exceptions set `result.upload_warning` and return early.
   - **Upload** (`:788`): `upload_recording(copy.scrubbed_dir, force)` uploads from the **scrubbed** dir; `recording.db` is never in the set.
   - **Mark uploaded** (`:800`): `_mark_uploaded_chunks` marks each remotely-confirmed, non-failed chunk `UPLOADED`.
   - **Sentinel** (`:817`): gated on `not copy.failed_chunks and result.finalize_gate_satisfied`; `_write_sentinel` (`:1034`) reads `chunks_expected` from the ledger (not a glob) and calls `upload_sentinel`.
   - **Retention** (`:830`): `_apply_retention` → `evict_recording`.

**Three entry points** all hold the same flock: live finalize at stop (`engine/collaborators.py:460`, runs its own legacy upload logic inside `terminal_lock`), manual `screencap upload` (`cli/__init__.py:2722`), and daemon resume (`daemon/supervisor.py:470`, `non_blocking=True`, wired but not auto-invoked).

### 9. Scrub / Redaction Layer (`scrubber.py`, `privacy/`, `video_mask.py`)

`scrub_recording(name, ...)` (`scrubber.py:3094`) never mutates the source. It acquires `recording_scrub_lock` (a distinct flock from the terminal lock), `copytree`s the source into `<name>-scrubbed/` (chmod `0o700`) excluding media + derived files, then runs `Scrubber.run()` (`:2114`):
- **Screenshots** masked/deleted by privacy disposition (Apple Vision OCR for text-redact / OCR-fallback frames).
- **`recording.db`** (in the copy): deletes `screenshot` + `audio_info` tables, NER-scrubs text/JSON columns of `action_event` + `window_event` + `recording.task_description`.
- **`events_*.jsonl`**, **transcripts**, **`system_metrics.json`** (rule-based host/SSID redaction).
- **Audit log** `privacy_audit.json` (`:2787`) — schema v1 with `entries`, `blocked_intervals`, `fail_closed`.
- **Sentinel** `.scrub_complete` with a `source_hash` so `is_scrubbed_copy_reusable` (`:3066`) can skip a rebuild.

**Detection pipeline** (`privacy/__init__.py:168`, `create_default_pipeline`): three composed detectors run on NFKC-normalized text — `RegexDetector` (`privacy/regex.py`), `DetectSecretsDetector` (the `detect-secrets` library, `privacy/secrets.py`), and `PiiDetector` (Presidio Analyzer with `fast-gliner` ONNX `knowledgator/gliner-pii-base-v1.0`, falling back to spaCy `en_core_web_sm`, `privacy/pii.py`). The `Anonymizer` replaces spans with `<ENTITY_TYPE>`.

**Fail-closed**: a failed text scrub yields the `"<SCRUB_FAILED>"` sentinel; unscrubbable files are renamed `*.scrub_failed` (which `upload.list_recording_files` excludes), or in the full-run events case deleted outright.

**`ScrubWorker`** (`privacy/scrub_worker.py:100`): a live thread that, on menu-bar app/domain **disable** events, retroactively deletes matching `screenshot`/`action_event`/`window_event` rows + on-disk screenshots from `recording.db` (with a 2s prelude for URL-detection lag), inside a `BEGIN IMMEDIATE` transaction + WAL checkpoint.

**Video masking** (`video_mask.py:341`, `mask_video_chunk`): gated by `get_masked_video_upload_enabled()` (**default OFF**). When ON, it requires dense window-geometry coverage (≤2s gaps, else `MaskOutcome.FAILED` — fail closed), then either produces a faithful copy (`UNMASKED_PROVABLY_SAFE`) or PyAV-transcodes with near-black rectangles over sensitive windows (`MASKED`). `CloudCopyProducer._mask_videos` (`terminal_stage.py:405`) maps the outcome onto the ledger (`mark_scrubbed` / `mark_failed`).

### 10. Upload Seam (`upload.py`)

- **`list_recording_files`** (`upload.py:212`): WAL-checkpoints first, then enumerates files using a **denylist** (`_is_raw_artifact`, `:181`) excluding `recording.db` + `-wal`/`-shm` sidecars and `*.scrub_failed`, plus dotfiles; largest-first.
- **`assert_uploadable`** (`:194`): hard-rejects raw artifacts with `ValueError` — enforced both in `list_recording_files` consumers and re-validated in `upload_recording` (`:380`).
- **`request_signed_urls`** (`:263`): POSTs `{recording, files:[{name, content_type}]}` to `SCREENCAP_UPLOAD_URL` (default a Cloud Run endpoint) with Firebase-auth `authed_post` (401 → refresh + retry). Returns `(urls, gcs_prefix)` where a `None` URL means "already in GCS" — this doubles as the **remote-existence probe** used by reconcile and retention re-confirm.
- **`upload_recording`** (`:338`) / `_upload_with_progress` (`:651`): streaming HTTP PUT to signed URLs via a `ThreadPoolExecutor` (default 4 workers), with 403 → re-sign retry and SIGTERM → `KeyboardInterrupt` cancel handling.

### 11. Retention & Eviction (`retention.py`)

`evict_recording(recording_dir, *, policy, ledger, remote_exists, ...)` (`retention.py:305`) is decoupled from upload — it consults ledger state + a fresh remote-confirm callback. Flow: resolve frozen policy/ledger → `_resume_pending` (finish interrupted `EVICT_PENDING` evictions first, re-confirming remote) → `_evict_masked_cloud_copies` (cloud/both only) → if not `KEEP_FOREVER`, `_evictable_candidates` (cloud: `UPLOADED` only; local: `LOCAL_DONE` only) → `_select_for_policy` (`DELETE_AFTER_UPLOAD` all candidates / `DELETE_AFTER_DAYS` via `_select_past_days` / `SIZE_CAP` via `_select_over_size_cap`) → `_evict_one` (two-phase `begin_eviction`/`begin_local_eviction` → `commit_eviction` with `EVICT_PENDING`-before-unlink).

The hard floor holds under every policy: only `UPLOADED` cloud chunks (re-confirmed remote NOW) or `LOCAL_DONE` local chunks are candidates. The only production caller is `_apply_retention` in `terminal_stage.py:835`, always as the final action of a terminal-stage pass, inside the already-held flock.

### 12. Daemon Orchestration (`daemon/`, `cli/`, `session.py`)

`screencap start` is a **thin HTTP client** (`cli/__init__.py:670` → `_run_start_via_daemon`, `:889`). It ensures a daemon (auto-spawning `screencap serve --idle-shutdown=600` via `os.posix_spawn` if no LaunchAgent — `cli/_autospawn.py:273`), POSTs `/v0/recording.start`, then streams `/v0/events` NDJSON, mapping `recording_finalized` to an exit code.

The **daemon** (`daemon/app.py` lifespan, `:33`) owns an `EventBus` and a `Supervisor`. `Supervisor.spawn()` (`daemon/supervisor.py:271`) claims the pidfile lock, allocates the capture dir, and launches the engine as a subprocess: `[python, -m, screencap, _engine-worker, <encoded_args>]`. The engine's `_engine_worker_cmd` (`cli/__init__.py:231`) emits `EVENT_STARTED` on stderr, calls `run_recording_worker` (`session.py:136`) → `start_recording` (`engine/screen_recorder.py`) → `RecordingCollaborators.start()` (which spawns `ChunkProcessor` + `ScrubWorker`), then emits `EVENT_RECORDING_FINALIZED`. The Supervisor's `_stderr_pump` (`:631`) bridges engine stderr events onto the EventBus; `_exit_poll`/`_handle_engine_exit` (`:649`/`:654`) synthesize a finalized event + mark the catalog `terminated_unexpectedly` on a crash.

`screencap stop` → `/v0/recording.stop` → `Supervisor.stop()` (`:367`): `proc.terminate()` (SIGTERM), waits for `EVENT_RECORDING_FINALIZED`, else `proc.kill()`. The engine's SIGTERM handler unwinds the recording loop and runs the live finalize path.

**`resume_terminal_stage`** (`:470`) is the daemon-restart resume seam — it dispatches `run_terminal_stage(..., non_blocking=True)` to a worker thread, skipping if the lock is held. The docstring (`:488`) notes it is **not auto-invoked from `_handle_engine_exit` in the current milestone**.

**`session.py:_postprocess_pipeline`** (`:351`) is the legacy post-recording pipeline (auto-export, auto-transcribe, auto-name, summary). It is invoked only via the old `SessionController` → `run_postprocess_worker` path (`session.py:297`), **not** on the daemon path; the daemon path's processing is handled by `ChunkProcessor` (live) and `run_terminal_stage` (post).

---

## Code References

### Capture engine
- `src/screencap/engine/recorder.py:3383` — `record()` core wiring (queues, readers, writers)
- `src/screencap/engine/recorder.py:211` — `event_processor` thread (route + fan-out)
- `src/screencap/engine/recorder.py:1503` — `screen_event_reader`
- `src/screencap/engine/recorder.py:4413` — `_chunk_fanout` thread
- `src/screencap/engine/extensions/synchronized_queue.py:51` — `SynchronizedQueue`
- `src/screencap/engine/video.py:771` — `ChunkedVideoWriter.write_frame` + rotation
- `src/screencap/engine/screen_recorder.py:329` — `_run_screen_recorder` lifecycle
- `src/screencap/engine/collaborators.py:302` — `ChunkProcessor` instantiation; `:460` finalize

### Processing pipeline
- `src/screencap/pipeline_state.py:244` — `PipelineLedger`; `:117-179` state enums; `:524-620` eviction; `:759` finalize gate
- `src/screencap/engine/db/models.py:107` — `PipelineChunkState` schema
- `src/screencap/pipeline_stages.py:107` — `PipelineStageRunner`; `:144` `run_chunk`
- `src/screencap/chunk_processor.py:557` — `_run_agnostic_stages`; `:820` `_transcribe`; `:503` `_mirror_status_to_ledger`
- `src/screencap/pipeline_policy.py:142` — `resolve_policy`; `:66` `ResolvedPolicy`; `:208` `set_default_override`
- `src/screencap/terminal_stage.py:526` — `run_terminal_stage`; `:191` `terminal_lock`; `:329` `CloudCopyProducer`; `:879` reconcile; `:1034` sentinel
- `src/screencap/scrubber.py:3094` — `scrub_recording`; `:2114` `Scrubber.run`; `:2171` `run_chunk`
- `src/screencap/video_mask.py:341` — `mask_video_chunk`
- `src/screencap/privacy/scrub_worker.py:100` — `ScrubWorker`
- `src/screencap/upload.py:212` — `list_recording_files`; `:194` `assert_uploadable`; `:263` `request_signed_urls`
- `src/screencap/retention.py:305` — `evict_recording`

### Orchestration
- `src/screencap/cli/__init__.py:670` — `start`; `:231` `_engine_worker_cmd`; `:2722` manual upload
- `src/screencap/cli/_autospawn.py:273` — `ensure_daemon_or_spawn`
- `src/screencap/daemon/app.py:309` — `recording_start`; `:410` `recording_stop`; `:553` `events_stream`
- `src/screencap/daemon/supervisor.py:271` — `spawn`; `:367` `stop`; `:470` `resume_terminal_stage`; `:570` `_reconcile`
- `src/screencap/session.py:136` — `run_recording_worker`; `:351` `_postprocess_pipeline`

---

## Architecture Documentation (Patterns & Conventions)

- **Disk is the source of truth.** Capture writes rich chunks to disk; every processing decision is reconstructable from `recording.db` + chunk files + the ledger. There is no fork at recording start — a single disk-first pipeline converges toward the destination.
- **Frozen-at-start policy.** Destination + retention are resolved once and serialized to `.recording_intent` (schema v2); in-flight recordings are immune to later config changes. `set_default_override` is the single seam for future plan tiers.
- **Idempotent + lock-serialized convergence.** `run_terminal_stage` can run any number of times; a per-recording advisory `fcntl.flock` (plus in-process lock) serializes live finalize, manual upload, and daemon resume. `non_blocking` callers raise/skip via `TerminalStageBusy`.
- **Ledger-enforced data-loss prevention.** Closed-set seeding, tri-state upload status, frozen `chunks_expected`, fresh-remote-confirm-before-delete, and `EVICT_PENDING`-before-unlink are enforced in `PipelineLedger` SQL transitions, not by convention.
- **`recording.db` is local-only by rule.** Upload enumeration excludes it + sidecars + `*.scrub_failed`; `assert_uploadable` hard-rejects it; cloud structured data derives only from scrubbed exports.
- **Fail-closed privacy.** Scrub failures yield `<SCRUB_FAILED>` sentinels and `*.scrub_failed` renames (un-uploadable); video masking distinguishes "provably safe" from "failed" to avoid silent no-ops; `masked_video_upload` defaults OFF.
- **Threads vs processes.** Readers/event-processor/ChunkProcessor/ScrubWorker are threads in the engine subprocess; writers (screen/action/window/video/audio/network) + the mitmproxy + the engine itself are separate processes. macOS spawn mode requires `apply_config_overrides()` re-application and pre-resolved PyObjC symbols.
- **Destination-agnostic vs destination-aware split.** `PipelineStageRunner` (transcribe/export/manifest) carries no local-vs-cloud knowledge; all routing lives in `terminal_stage` + `retention`.

---

## Historical Context (from docs/)

The canonical design and motivation:
- `docs/plans/2026-06-05-002-refactor-unified-recording-processing-pipeline-plan.md` — **the primary architecture doc** for the disk-first pipeline (PipelineLedger U1, `recording.db` local-only U2, policy resolver U3, stage runner, terminal stage, rules R1–R16/U1–U9/AE8/AE12). Status: completed. (Note: this file is currently untracked in git — the plan that produced the code being documented.)
- `docs/brainstorms/2026-06-05-unified-recording-processing-pipeline-requirements.md` — the requirements that drove it (today-vs-target shape, per-chunk lifecycle, acceptance criteria).
- `docs/solutions/runtime-errors/chunk-upload-sentinel-gating-and-data-loss.md` — root-cause of four chunk-upload data-loss bugs (no sentinel gate, `_stop_event` hole, stale sentinel on recovery, "disabled" conflated with "succeeded"); these became the foundational data-loss rules now codified in the ledger.

Scrub / upload seam:
- `docs/plans/2026-06-03-002-feat-native-redaction-review-upload-plan.md` — scrub-before-upload, `CloudCopyProducer`, review-data envelope, upload reuse-guard. Completed.
- `docs/plans/2026-05-29-001-refactor-pyav-review-pipeline-plan.md` — PyAV-native in-process video path (upstream of the scrubbed-copy production). Completed.
- `docs/solutions/research/scr-28-privacy-filter-vs-gliner.md` — decision to keep GLiNER for text-PII scrubbing.
- `docs/plans/2026-05-29-002-feat-per-user-cloud-storage-isolation-plan.md` + `004-feat-scr-28-...` — upload auth seam + GCS isolation/migration.

Daemon supervision layer:
- `docs/plans/2026-05-08-001-feat-daemon-architecture-phase-1-plan.md` (completed) + `2026-05-08-002-...-phase-2-plan.md` (active) — `screencap serve`, UNIX socket API, engine subprocess supervision, crash recovery, auto-spawn fallback.

Open follow-ups tracking the cutover (now Linear tickets in the Screencap team; the original `docs/todos/refactor/unified-recording-pipeline/` docs were migrated to Linear on 2026-06-09 and removed):
- **SCR-125** (High) — Cut live finalize + manual upload over to `run_terminal_stage` (the keystone cutover; blocks SCR-126).
- **SCR-126** (High, blocked by SCR-125) — Masked-video-upload flag enablement prerequisites & fail-open hardening.
- **SCR-127** (Medium) — Terminal reconcile should re-validate already-`UPLOADED` rows, not only `PENDING`/`FAILED`.
- **SCR-128** (Low) — Pipeline efficiency (redundant GCS probes, per-op ledger connects) & convention de-duplication.

## Related Research

No prior documents were found under `docs/research/`; this is the first research document in that directory for this topic.

## Open Questions

These are observations about the *current* state (documentation, not critique) that a reader may want to confirm against intent:

1. **Two live drivers coexist.** `ChunkProcessor` (legacy live upload during recording) and `run_terminal_stage` (disk-driven convergence) both exist; the live finalize path (`engine/collaborators.py:460`) still runs its own legacy upload logic inside `terminal_lock` rather than calling `run_terminal_stage`. The cutover is tracked in todo `001`.
2. **`resume_terminal_stage` is wired but not auto-invoked** from `_handle_engine_exit` (`daemon/supervisor.py:488` docstring) — daemon-restart resume currently requires an explicit caller.
3. **`_postprocess_pipeline`** (`session.py:351`) remains in the codebase but is only reached via the old `SessionController` path, not the daemon path used by `screencap start`.
4. **Manifest v2 has no file-level idempotency gate** (`task_manifest.py:61`) — it re-queries and re-writes on each run, unlike v1; this is intentional per the code but worth noting for anyone reasoning about stage idempotency.
