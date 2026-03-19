---
title: "Chunk upload sentinel gating, timeout accounting, and silent data loss"
date: 2026-03-19
problem_type: runtime
component: chunk_processor, recorder, upload pipeline
symptoms:
  - "recording_complete.json uploaded to GCS before chunk data arrives"
  - "Cloud Run stitching triggered with missing chunks (chunks_expected mismatch)"
  - "all_chunks_uploaded() returns True despite timed-out or unprocessed chunks"
  - "stub_recording() deletes local .mp4/.flac files when nothing was uploaded to GCS"
  - "User sees 'Recording complete' with no warning when data is silently lost"
  - "Recovery sentinel has chunks_expected: 0 because manifests hadn't been generated yet"
root_causes:
  - "Sentinel upload was unconditional — no gate on chunk upload success"
  - "Force-stop timeout skipped adding chunks to _chunk_results — all_chunks_uploaded() only checked present entries (survivorship bias)"
  - "Recovery path reused stale sentinel file instead of regenerating from current manifest count"
  - "Privacy pipeline failure set _upload_enabled=False but chunks still marked success=True, allowing stub_recording() to delete local files"
tags:
  - state-consistency
  - fail-closed
  - silent-data-loss
  - upload-pipeline
  - sentinel-gating
  - shutdown-path
  - timeout-handling
  - chunk-processor
  - defense-in-depth
---

# Chunk upload sentinel gating, timeout accounting, and silent data loss

## Context

Screencap is a macOS CLI tool that records screen activity. For cloud-intent recordings (`--cloud`), a `ChunkProcessor` background thread processes video chunks as they're produced: transcribing audio, exporting events to JSONL, generating manifests, scrubbing PII, and uploading each chunk to GCS via signed URLs. The `_chunk_results: dict[int, bool]` dictionary tracks whether each chunk was uploaded.

When recording stops, `recorder.py` calls `chunk_processor.stop()`, then decides whether to upload `recording_complete.json` (the "sentinel") to GCS. Cloud Run watches for this file to trigger the stitching pipeline that assembles chunks into a final timeline. After confirming the sentinel uploaded, `stub_recording()` deletes local media files to free disk space.

The sentinel is the point of no return: once uploaded, Cloud Run starts stitching with whatever chunks are available. And `stub_recording()` is the point of no recovery: once local files are deleted, only GCS copies remain.

## Problem

Four related bugs in the recording shutdown path, all stemming from incorrect assumptions about chunk upload completeness:

**Bug 1 — Orphaned sentinel (fixed):** `recording_complete.json` was uploaded unconditionally after `chunk_processor.stop()`, regardless of whether chunks had actually uploaded. Cloud Run would trigger stitching with missing data.

**Bug 2 — Force-stop blindspot (fixed):** If `chunk_processor.stop(timeout=300)` timed out, `_stop_event` was set, and the in-flight chunk returned early from `_process_chunk()` without writing to `_chunk_results`. With 6 of 7 chunks done, `all_chunks_uploaded()` returned `True` over only the 6 entries (survivorship bias). Then `stub_recording()` deleted the 7th chunk's `.mp4` — permanent data loss.

**Bug 3 — Stale recovery sentinel (fixed):** The degraded shutdown path wrote a local `recording_complete.json` with `chunks_expected` from the current manifest count on disk. If manifests hadn't been generated yet, `chunks_expected` was 0. `screencap upload` only regenerates the sentinel when the file is absent, so the stale sentinel was uploaded unchanged.

**Bug 4 — Silent data loss on privacy failure (found, not yet fixed):** When the privacy scrubbing pipeline fails to initialize, `_upload_enabled` is set to `False`. But `_process_chunk()` treats this as `success = True` (line 268: `upload disabled = success`). All chunks are marked uploaded, sentinel goes to GCS, `stub_recording()` deletes local files. Nothing is on GCS — recording is silently destroyed.

## Root Cause Analysis

### The shared root cause: incomplete state treated as authoritative

`_chunk_results` is an append-only dict populated as chunks complete processing. It only contains entries for chunks that *reached the result-recording code*. Multiple consumers — sentinel upload, `stub_recording()`, user messaging, `all_chunks_uploaded()` — all treat it as a complete picture. When entries are missing (timeouts, early returns) or semantically wrong ("disabled" treated as "success"), every downstream decision is wrong.

### Bug 1: No gate between chunk upload and sentinel upload

```
chunk_processor.stop()  →  upload_sentinel()  →  stub_recording()
                            ↑ no check here
```

The sentinel upload at `recorder.py:999-1013` ran unconditionally after `stop()` returned. There was no check of `all_chunks_uploaded()` before uploading.

### Bug 2: _stop_event causes invisible hole in _chunk_results

```
stop(timeout=300)
  → _thread.join(300)  — times out
  → _stop_event.set()
  → _process_chunk() hits: if self._stop_event.is_set(): return
  → _chunk_results[idx] never written  ← invisible hole
  → all_chunks_uploaded() → all({0:T, 1:T, ..., 5:T}.values()) → True
  → stub_recording() deletes chunk_0006.mp4 (never uploaded)
```

### Bug 3: Stale sentinel survives recovery

```
Degraded shutdown:
  _n_chunks = len(glob("chunk_*_manifest.json"))  → 0 (not generated yet)
  write recording_complete.json with chunks_expected: 0

Later: screencap upload
  _recover_chunk_metadata()  → generates missing manifests
  if not sentinel_path.exists():  → False (stale file exists)
  → stale sentinel uploaded with chunks_expected: 0
```

### Bug 4: "Disabled" conflated with "succeeded"

```
ChunkProcessor.__init__():
  privacy pipeline fails → self._upload_enabled = False

_process_chunk():
  if self._upload_enabled:     # False
      ...
  else:
      success = True           # ← "disabled" treated as "success"

  self._chunk_results[idx] = True  → all_chunks_uploaded() → True
  → sentinel uploaded, stub_recording() deletes local files
  → nothing on GCS, nothing local → data gone
```

## Investigation Steps

1. Traced the post-stop shutdown sequence in `recorder.py` and found `upload_sentinel()` ran unconditionally after `chunk_processor.stop()` — no check of `all_chunks_uploaded()` before uploading.
2. Implemented the gate: sentinel only uploads when `all_chunks_uploaded() and _n_chunks > 0`. Added `upload_summary()` for specific user messaging ("6 of 7 chunks uploaded").
3. Code review flagged `except Exception: pass` on local sentinel write (silent swallow) and misleading `_n_processed` variable name — fixed both.
4. Deeper review traced `stop()` timeout path: `_stop_event.set()` → `_process_chunk()` early return → `_chunk_results[idx]` never written → `all_chunks_uploaded()` returns `True` over only the completed entries (survivorship bias). Confirmed `stub_recording()` would delete the un-uploaded .mp4.
5. Same review traced the `else` branch local sentinel write → `screencap upload` recovery skipping regeneration because file already exists → stale `chunks_expected: 0` uploaded to GCS. Fix: removed the local sentinel write entirely.
6. Added `was_force_stopped` property to `ChunkProcessor`, factored into `_all_uploaded` guard. Both sentinel upload and `stub_recording()` now gated behind it.
7. Manual end-to-end test with `screencap start --cloud --chunk-duration 10` revealed Bug 4: privacy masking classifier failed to init → `_upload_enabled = False` → all chunks marked `success = True` → `stub_recording()` deleted local files → `gsutil ls` showed nothing on GCS. Recording silently destroyed with no user warning.

## Working Solution

### Fix 1: Gate sentinel upload behind all_chunks_uploaded()

At `recorder.py:999-1029`, the sentinel now only uploads when all chunks are confirmed uploaded and at least one manifest exists:

```python
# recorder.py:1003-1006
_all_uploaded = (
    chunk_processor.all_chunks_uploaded()
    and not chunk_processor.was_force_stopped
)

# recorder.py:1010-1011
if cloud_intent and live_upload:
    if _all_uploaded and _n_chunks > 0:
        # upload sentinel
    # else: no local sentinel — screencap upload generates fresh one
```

### Fix 2: was_force_stopped prevents trusting incomplete _chunk_results

```python
# chunk_processor.py:146-153
@property
def was_force_stopped(self) -> bool:
    """True if stop() timed out and had to force-stop the thread."""
    return self._stop_event.is_set()
```

When `True`, `_all_uploaded` is `False` regardless of `_chunk_results` contents. This prevents both sentinel upload and `stub_recording()` from running.

### Fix 3: Removed stale local sentinel write

The degraded shutdown `else` branch no longer writes a local `recording_complete.json`. The `screencap upload` recovery path at `cli.py:1401-1417` already generates a fresh sentinel with the correct `chunks_expected` from current manifests on disk when the file is absent.

### Bug 4: Not yet fixed

The fix requires distinguishing "uploads intentionally disabled" from "uploads disabled due to error." Proposed: add `_upload_disabled_reason: str | None` to ChunkProcessor. When `None`, disabled is intentional (`success = True`). When set, disabled is due to failure (`success = False`), preventing `stub_recording()` from deleting local files.

## Prevention Strategies

### 1. Track the full universe of expected work

`_chunk_results` should be initialized with all expected chunk IDs mapped to `PENDING`, not built up incrementally. `all_chunks_uploaded()` on a closed set cannot exhibit survivorship bias — missing entries are impossible.

### 2. Distinguish "disabled" from "succeeded"

A subsystem being disabled (privacy failure) is not the same as succeeding. Use tri-state results (`UPLOADED | SKIPPED | FAILED`), not booleans. Only `UPLOADED` should permit local file deletion.

### 3. Never delete local files without confirming remote existence

`stub_recording()` trusts internal bookkeeping (`_chunk_results`). Defense-in-depth: verify remote file existence (lightweight GCS stat call per chunk) before deleting local copies. The cost is negligible vs. irreversible data loss.

### 4. Test degraded paths, not just happy paths

Every code path that deletes data needs a test where the precondition (remote data exists) is false, verifying deletion does NOT occur. Test `all_chunks_uploaded()` with incomplete sets (missing entries), not just all-true or all-false.

### 5. Sentinel is the last write, gated on all prior writes

The completeness signal must be derived from completeness evidence. If any chunk is not confirmed uploaded, the sentinel must not be uploaded.

## Related Documentation

- `docs/solutions/runtime-errors/sigint-handler-timing-and-recording-stop-methods.md` — Documents signal handler timing and all 8 recording stop mechanisms. The sentinel gating bug is a downstream consequence of the same shutdown pipeline.

**Source files changed:**
- `src/screencap/recorder.py` — sentinel gating, force-stop guard, stale sentinel removal
- `src/screencap/chunk_processor.py` — `upload_summary()`, `was_force_stopped` property
- `tests/test_chunk_processor.py` — integration tests for 3 upload gating scenarios

**Key commits:**
- `530f8cc` — fix: gate sentinel upload behind all_chunks_uploaded()
- `dc4fcff` — fix: prevent sentinel upload after force-stop and remove stale local sentinel
- `55580d1` — chore: update capture-test skill to target changed code paths
- `5dd00ed` — feat: upload sentinel after graceful recording stop to trigger stitching (original sentinel feature)
- `ec7912e` — feat: graceful stop via SIGTERM, hard exit, and sentinel recovery in CLI
