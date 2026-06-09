---
title: "Pipeline efficiency (redundant GCS probes, per-op ledger connects) and convention de-duplication"
status: pending
priority: low
created: 2026-06-08
source: code-review of refactor/unified-recording-pipeline (efficiency + reuse angles)
related_plans:
  - docs/plans/2026-06-05-002-refactor-unified-recording-processing-pipeline-plan.md
---

# Pipeline efficiency & convention de-duplication

Non-blocking cleanups on the (currently non-default) cloud terminal path.

## Efficiency

- **Redundant per-chunk GCS probes.** Within one cloud terminal run, the same chunk is re-stat'd via `_chunk_confirmed_remote` -> `request_signed_urls` up to 3× (reconcile step 1, `_mark_uploaded_chunks` step 4, retention `begin_eviction` confirm). Cache the per-chunk confirm result for the duration of one flock-held run.
- **Sequential probes.** Reconcile / mark loops issue one blocking `request_signed_urls` per chunk serially; a bounded thread pool would cut finalize latency (and the window other entry points block on the flock) for large recordings.
- **Per-op `sqlite3.connect` in `PipelineLedger`.** Every op opens+PRAGMA+closes a connection; staging/finalize do hundreds of cycles over `recording.db`. A per-instance cached connection (the class already holds a `threading.Lock`) would amortize it while keeping the busy_timeout/WAL discipline. Also `_route_cloud` calls `all_chunks()` 3× back-to-back (`_refresh_counts`, `all_uploaded`, `finalize_gate_satisfied`) — load once.
- **`video_mask` per-frame PIL roundtrip.** `_transcode_with_masks` does `to_image()`/`from_image()` for every frame even when no interval applies; unmasked frames could re-mux the original packet (or skip the PIL roundtrip).

## Duplicated load-bearing conventions (the exact class of bug as the fixed GCS-key mismatch)

- Chunk "core files" `[chunk/audio/events/manifest]_NNNN` open-coded in `terminal_stage._chunk_confirmed_remote`, `retention._chunk_local_files`, and `chunk_processor._collect_chunk_files`. Extract `chunk_core_filenames(idx)`.
- `<name>-scrubbed` sibling-dir derivation in `terminal_stage` (×2), `retention`, and `scrubber` (a 4th way). Add `scrubber.scrubbed_dir_for(recording_dir)` and call it everywhere (`masked_video_dir` already lives there).
- `_open_ledger` (schema-migrate-then-open) duplicated in `terminal_stage` and `retention`; `_make_remote_confirm` / `_make_promotion_confirm` are the same closure 2-3×. Consolidate (ideally into `pipeline_state`).
- `video_mask._MASK_FILL` duplicates `privacy/masking._MASK_COLOR`; import instead of hand-syncing.
- `terminal_stage._chunk_timing` reimplements `recovery.py`'s chunk-range derivation and can diverge on the last-chunk boundary (recovery extends to `last_ts+1.0`). Share one range helper.
- `retention._ledger_age_reader` reaches into `PipelineLedger._db_path`/`_recording_id` (private); add a public age accessor on the ledger.
