---
title: "feat: Network proxy logging V1.75 — cloud upload integration"
status: active
created: 2026-05-14
type: feat
linear: https://linear.app/zk-email/issue/SCR-12/network-proxy-logging-v175-cloud-upload-integration
branch: rutefig/scr-12-network-proxy-logging-v175-cloud-upload-integration
depth: deep
---

# feat: Network proxy logging V1.75 — cloud upload integration

## Summary

V1.75 wires network proxy events into the live cloud upload pipeline that already ships action and window events. The chunk processor extends `_export_events()` to query `network_event` rows for each chunk's time range, decrypt and scrub bodies inline through a per-recording `NetworkScrubPipeline` cached for the processor's lifetime, then interleave the resulting events with action and window events sorted on `timestamp_ns`. A new `NetworkHealth` table records proxy lifecycle and failure events; the chunk processor reads it at export to mark chunks that overlap a crash as `NETWORK_INCOMPLETE`. The `_chunk_results: dict[int, bool]` map migrates to `dict[int, ChunkStatus]` with explicit `PENDING | EMITTED | FAILED | NETWORK_SKIPPED | NETWORK_INCOMPLETE` states so the sentinel-upload gate stops conflating "uploaded" with "disabled but truthy".

The migration is the safety-critical core: `docs/solutions/runtime-errors/chunk-upload-sentinel-gating-and-data-loss.md` documents four production incidents caused by exactly the survivorship-bias and "disabled-treated-as-success" patterns the new enum closes off permanently. Every existing reader of `_chunk_results` shifts meaning under the migration (all enum members are truthy) and must be rewritten to compare explicitly against `ChunkStatus.EMITTED`.

## Problem Frame

**Today (post-V1.5):** Network events live in `recording.db.network_event` with `body_ciphertext` encrypted at rest. The V1.5 `NetworkScrubPipeline` decrypts and scrubs bodies at export time, and the engine's `unified_export_events` already plumbs `network_rows` + `network_scrub_pipeline` parameters end-to-end. Explicit `screencap export` consumes this via `cli.py:1538-1599`. **The chunk processor's `_export_events()` does not.** Network events never reach the JSONL files that the cloud pipeline uploads — they sit in the recording DB and never leave the machine.

**V1.75 closes the gap** by making the chunk processor the second consumer of the V1.5 export plumbing. It additionally formalises:

- **Failure observability** — V1 had no record of proxy lifecycle. A crashed proxy mid-recording shipped chunks that silently missed network coverage.
- **Status tri-state** — booleans cannot distinguish "uploaded", "intentionally skipped (no encryption available)", and "incompletely captured (proxy died)" without conflation, and the prior incidents prove the cost of getting this wrong.
- **Mode semantics** — explicit `screencap export` should fail loud on KEK unavailability (user asked to see bodies). Chunk export should fail soft (preserve a recording rather than abort the upload pipeline mid-flight).

## Origin Document

Linear SCR-12 — [Network proxy logging V1.75 — cloud upload integration](https://linear.app/zk-email/issue/SCR-12/network-proxy-logging-v175-cloud-upload-integration). V1.5 (SCR-11) shipped in PR #157 and is the baseline this plan builds on. The Linear issue references `docs/plans/2026-04-25-003-feat-network-proxy-logging-plan.md` and `docs/tickets/medium-2026-04-27-feat-network-logging-v1.75-cloud-upload.md`; **neither file exists in the current repo state** — they were either not committed or rotated out. This plan supersedes those references.

---

## Pre-Implementation Gates

These gates **must clear before go-live** (merge to `main` and cloud rollout). They are blocking decisions for *shipping*, not for *starting implementation* — U1–U9 may land behind a config flag that defaults off; the gates control when that flag flips.

### Gate 1 — Cloud bucket policy brainstorm

Linear flags this as the V1.75 ship-blocker: *"Without these, V1.75 cannot ship — uploaded JSONL is plaintext post-scrub and the bucket is the authorization boundary."* The five sub-decisions:

1. Bucket name + GCP project + ownership
2. Read-access policy (engineers / service accounts / signed-URL only)
3. Retention + deletion lifecycle (post-scrub plaintext exposure window)
4. TLS-in-transit + encryption-at-rest enforcement (verify on existing screencap bucket; don't assume)
5. Audit log writes (who read what, when)
6. **Object integrity** — GCS object versioning + Object Retention / Bucket Lock; CRC32C/MD5 checksum verification on PUT and on read; policy that in-place object replacement is impossible without explicit versioned delete. The audit log under sub-decision 5 records read access but does not detect silent object replacement unless versioning is enabled.

**Action:** Run a separate `/ce-brainstorm` session to lock these. Output is a requirements doc in `docs/brainstorms/`; this plan does not start until that doc lands and is referenced here.

### Gate 2 — V1.5 false-negative analysis (acknowledged + deferred)

Linear flags this as a pre-V1.75 decision: bodies passing scrub but containing missed PII reach cloud. **Deferred** to a follow-up ticket. V1.75 ships with the current `DetectionPipeline` confidence posture; the accepted risk is documented in Scope Boundaries below. A follow-up ticket (out-of-scope here) covers threshold tuning.

---

## Resolved Planning Decisions

| Decision | Choice | Rationale |
|---|---|---|
| `session_start` event vs DB lookup | **DB lookup (Path A)** | Reuses V1.5 helpers (`recording_has_encrypted_bodies`, `NetworkScrubPipeline`); chunks carry post-decrypt scrubbed plaintext in `body_text`, so "self-describing chunks" buys nothing for downstream consumers. No new event type. |
| KEK-missing failure mode | **`METADATA_ONLY` fail-soft for chunk processor; `REQUIRE_DECRYPT` fail-loud for explicit `screencap export`** | Recording survival > body fidelity at chunk time; user-initiated export should surface a clear error so the user can remediate. |
| DetectionPipeline confidence threshold | **Defer to follow-up ticket** | See Gate 2. |
| Sort key for interleave | **`timestamp_ns` with fallback to `timestamp`** | Current code keys on `timestamp` (float seconds) at two sites; Linear specifies `timestamp_ns` for stable ordering under WebSocket bursts within 10 ms windows. This is a real correctness fix piggy-backed on V1.75. |
| `_chunk_results` PENDING initialisation | **Eager on rotation message receipt (before `_process_chunk` runs)** | True "expected universe at start" requires knowing chunk count up-front, which is not available pre-rotation. Setting `PENDING` eagerly the moment a rotation message arrives — *before* `_process_chunk(msg)` is invoked — preserves the survivorship-bias fix (force-stop leaves a `PENDING` entry that the gate explicitly rejects) without restructuring the rotation pipeline. |
| `NetworkHealth` write transport | **Direct DB writes (raw `sqlite3` or unbuffered ORM commit), not EventBus** | `eventbus-late-listener-replay-2026-05-12.md` documents late-listener races. DB-only writes side-step the race entirely; the chunk processor reads from the DB at export, no subscription needed. |

---

## System-Wide Impact

| Surface | Change |
|---|---|
| `src/screencap/engine/db/models.py` | New `NetworkHealth` SQLAlchemy model |
| `src/screencap/engine/db/__init__.py` | Extend `_ensure_network_tables` to create the new table idempotently |
| `src/screencap/engine/db/crud.py` | New `insert_network_health` helper (template: `insert_network_event_meta`) |
| `src/screencap/network/export_pipeline.py` | New `NetworkExportMode` enum alongside `KekUnavailableError` |
| `src/screencap/chunk_processor.py` | `_chunk_results` type migration; `_export_events` rewrites; sentinel + auto-delete gates re-keyed on `ChunkStatus.EMITTED` |
| `src/screencap/engine/collaborators.py` | 5 sites that read `_chunk_results` via public API — sentinel gate, stub_recording gate, summary message |
| `src/screencap/engine/network_policy.py` | Emit `proxy_started` `NetworkHealth` row on `_DefaultPolicy.setup()` |
| `src/screencap/engine/recorder.py` (or proxy reader) | Emit `proxy_crashed` / `network_writer_failed` `NetworkHealth` rows |
| `src/screencap/engine/export.py` + `src/screencap/engine/processing.py` | Sort + interleave keyed on `timestamp_ns` with float-second fallback |
| `src/screencap/cli.py` + `src/screencap/exporter.py` | Pass `NetworkExportMode.REQUIRE_DECRYPT` explicitly on `screencap export` paths |
| `tests/test_chunk_processor.py` | 18+ sites that read/write `_chunk_results` directly migrate to `ChunkStatus`; invert `test_v1_scope_guard_no_network_lines_in_chunk_jsonl` into V1.75 positive control |
| `pyinstaller/screencap.spec` + `_smoke-test` | Add `NetworkScrubPipeline.__init__()` smoke check (frozen-binary trap from `pyinstaller-frozen-binary-ci-failures.md`) |

**Affected parties:** end users (no UI change in V1.75 — `upload_summary()` continues to return the existing "N of M chunks uploaded" shape, where N counts EMITTED only and M counts every chunk regardless of network status; the `(network: SKIPPED)` / `(network: INCOMPLETE)` annotation in chunk-progress messages is **explicitly deferred** to follow-up work per Scope Boundaries); cloud pipeline (now receives `network.*` event lines in JSONL — Cloud Run stitching must tolerate them, verify before flipping); developers running explicit `screencap export` (will see fail-loud on KEK unavailability where they previously saw silent absence).

---

## High-Level Technical Design

This sketch communicates the intended flow at chunk export. **Directional guidance, not implementation specification.**

```
ChunkProcessor (per recording, long-lived for the recording)
│
├─ start():
│    self._network_scrub_pipeline = None
│    self._network_scrub_attempted = False
│    self._network_export_mode = NetworkExportMode.METADATA_ONLY
│
└─ _export_events(idx, start_ts, end_ts):
     1. Fetch network_event rows where timestamp_ns ∈ [start_ns, end_ns)
        via raw sqlite (open_recording_db) — matches export.py style
     2. If rows present AND not self._network_scrub_attempted:
          self._network_scrub_attempted = True
          if recording_has_encrypted_bodies(db_path, recording_id):
              try:
                  self._network_scrub_pipeline = NetworkScrubPipeline(db_path, recording_id)
              except KekUnavailableError:
                  # METADATA_ONLY fail-soft — record once, persist warning
                  insert_network_health(event="kek_unavailable", ...)
                  # pipeline stays None → bodies emit as null
     3. Fetch NetworkHealth rows where event in {proxy_crashed, network_writer_failed}
        AND timestamp_ns ∈ [start_ns, end_ns)
     4. Call export_chunk_events(..., network_rows=rows,
                                 network_scrub_pipeline=self._network_scrub_pipeline)
     5. Pre-compute pending status into `self._pending_network_status[idx]`:
          - rows present + pipeline None (KEK missing or prior failure) → setdefault NETWORK_SKIPPED
          - any health overlap (proxy_crashed / network_writer_failed) → unconditional NETWORK_INCOMPLETE
            (precedence over NETWORK_SKIPPED via unconditional overwrite)
     6. `_process_chunk` final-assignment (U3 contract) reads `_pending_network_status.pop(idx)`:
          - pending present → that value (NETWORK_SKIPPED or NETWORK_INCOMPLETE)
          - pending absent + upload success → EMITTED
          - pending absent + upload failure → FAILED
          - exception during _process_chunk → FAILED (outer handler consults pending — see U3)
```

All status decisions route through `_pending_network_status` so U4's KEK-failure write and U5's overlap write coordinate via a single in-memory seam; the final state is written to `_chunk_results` only after upload completes.

### ChunkStatus state machine

```
              ┌───────────┐
              │  PENDING  │ ← initialised on rotation message receipt
              └─────┬─────┘
                    │ _process_chunk completes
                    ↓
      ┌────────────────────────────────┐
      ↓                                ↓
  ┌────────┐                      ┌────────┐
  │EMITTED │  ← happy path        │ FAILED │ ← exception or upload failure
  └────────┘                      └────────┘
      ↑
      │ all network rows scrubbed
      │ AND no NetworkHealth overlap
      │
  ┌─────────────────┐    ┌──────────────────────┐
  │ NETWORK_SKIPPED │    │ NETWORK_INCOMPLETE   │
  └─────────────────┘    └──────────────────────┘
   KEK unavailable        proxy_crashed or
   at first chunk         network_writer_failed
                          overlaps chunk window
```

**Sentinel-upload gate predicate** (replaces `chunk_processor.py:153-161`):

```
def all_chunks_uploaded(self) -> bool:
    if not self._chunk_results:
        return False
    # Explicit EMITTED-only — NETWORK_SKIPPED, NETWORK_INCOMPLETE, FAILED,
    # PENDING all block the gate. Per docs/solutions/runtime-errors/
    # chunk-upload-sentinel-gating-and-data-loss.md fix #4: disabled ≠ succeeded.
    return all(s == ChunkStatus.EMITTED for s in self._chunk_results.values())
```

---

## Implementation Units

### U1. NetworkHealth model + idempotent table creation

**Goal:** Foundation for proxy lifecycle observability. Add the SQLAlchemy model + table-create hook + CRUD helper. Failure-only, low-frequency writes.

**Requirements:** Linear "`NetworkHealth` table (FK to recording.id)" requirement.

**Dependencies:** None.

**Files:**

- `src/screencap/engine/db/models.py` — new `NetworkHealth` class
- `src/screencap/engine/db/__init__.py` — extend `_ensure_network_tables`
- `src/screencap/engine/db/crud.py` — new `insert_network_health` (template: `insert_network_event_meta` at `:227`)
- `tests/test_engine_db.py` (or equivalent) — model + idempotent-create coverage

**Approach:**

- Model fields: `id` PK; `recording_id` FK to `recording.id` with `ondelete="CASCADE"`; `event` enum column accepting `proxy_started | proxy_crashed | kek_unavailable | network_writer_failed`; `timestamp_ns` BigInt NOT NULL; `details` TEXT nullable.
- Use SQLAlchemy's `Enum(..., native_enum=False)` (SQLite has no native enums) with the four string values. Add a `__table_args__` index on `(recording_id, timestamp_ns)` for the chunk overlap query.
- Add `Recording.network_health` relationship (back_populates) and order_by `timestamp_ns`.
- Extend `_ensure_network_tables` to add a third `__table__.create(engine, checkfirst=True)` for `NetworkHealth`. Return value semantics unchanged (False on readonly DB).
- `insert_network_health(session, recording, event, timestamp_ns, details=None)` follows `insert_network_event_meta`: unbuffered, immediate `session.commit()`. Failure events must persist before any subsequent crash.

**Patterns to follow:** `NetworkEventMeta` in `engine/db/models.py:400` and `insert_network_event_meta` in `engine/db/crud.py:227`.

**Test scenarios:**

- `test_network_health_model_columns`: round-trip insert + select returns expected values; enum constraint rejects invalid strings.
- `test_network_health_cascade_on_recording_delete`: deleting a `Recording` cascades and removes its `NetworkHealth` rows.
- `test_ensure_network_tables_creates_network_health`: call on fresh DB → table exists; call again → idempotent (no error).
- `test_ensure_network_tables_readonly_db_returns_false`: covers the existing readonly contract for the new table.
- `test_insert_network_health_commits_immediately`: insert then simulate crash before further work → row persists.
- Edge case: `details` field accepts > 1 KB strings without truncation (failure stack traces).

**Verification:** Run `pytest tests/test_engine_db.py -v`. `_ensure_network_tables` returns `True` on a fresh recording DB and idempotency holds across repeated `Capture.load()` calls.

### U2. NetworkExportMode enum

**Goal:** Formalise the fail-loud vs fail-soft contract between explicit export and chunk export.

**Requirements:** Linear "`NetworkExportMode` enum (`REQUIRE_DECRYPT` ... `METADATA_ONLY` ...)" requirement.

**Dependencies:** None (independent of U1).

**Files:**

- `src/screencap/network/export_pipeline.py` — add enum next to `KekUnavailableError` (line 52)
- `tests/test_network_export_pipeline.py` — mode-behaviour coverage

**Approach:**

- `class NetworkExportMode(str, Enum)` with `REQUIRE_DECRYPT = "require_decrypt"` and `METADATA_ONLY = "metadata_only"`.
- The enum is **declarative only in U2** — callers in U4 (chunk processor) and U8 (CLI / `exporter.py`) consume it. Adding it independently first means U4 and U8 can land in parallel without circular dependency.
- Docstring on the enum class must spell out the fail-loud vs fail-soft semantics and reference Gate 1 / Gate 2 in this plan for the upstream rationale (pattern from `docs/solutions/integration-issues/mitmproxy-ignore-hosts-tunneling-all-flows-2026-04-29.md` — pin the rationale at the gate).

**Patterns to follow:** existing enum-in-module pattern in `screencap.engine.events.EventType`.

**Test scenarios:**

- `test_network_export_mode_values`: both members exist and serialise to expected strings.
- `test_network_export_mode_is_str_enum`: `NetworkExportMode.REQUIRE_DECRYPT == "require_decrypt"` (string-comparable for legacy config flows if any).
- Documentation test: docstring renders without syntax errors (mostly aesthetic — covered by ruff anyway).

**Verification:** Import the enum from a fresh REPL via `from screencap.network.export_pipeline import NetworkExportMode`. Both members reachable.

### U3. ChunkStatus enum + `_chunk_results` migration

**Goal:** Replace `dict[int, bool]` with `dict[int, ChunkStatus]`. Audit every reader and writer. This is the **safety-critical migration** — the four bugs in `chunk-upload-sentinel-gating-and-data-loss.md` are exactly the bugs the new enum prevents permanently.

**Requirements:** Linear "`_chunk_results: dict[int, bool]` → `dict[int, ChunkStatus]`" requirement + "migration audit before this lands" requirement.

**Dependencies:** None (no external prerequisites). **Sequencing constraint:** U3 declares the `__init__` state (`_chunk_results`, `_pending_network_status`, `_recording_id`, `_network_scrub_pipeline`, `_network_scrub_attempted`) that U4 and U5 write to and that U6's emission sites read from indirectly via the `chunk_processor.py` surface. Sequence U3 first; U4, U5, U6 all depend on its declarations even though they list other functional dependencies. U7 and U8 are independent.

**Files:**

- `src/screencap/chunk_processor.py` — `ChunkStatus` enum definition; rewrite all 9 internal sites; `all_chunks_uploaded`, `upload_summary`, `reconcile_against_gcs`, `_delete_old_chunks`, `_process_chunk`, `_run` exception handler all updated; new `__init__` fields (`_pending_network_status`, `_recording_id`, `_network_scrub_pipeline`, `_network_scrub_attempted`)
- `src/screencap/engine/collaborators.py:503-578` — 5 sites that consume the public API gate sentinel upload and stub_recording
- `tests/test_chunk_processor.py` — 13+ direct dict-write/read sites migrate; assertions switch to enum equality

**Approach:**

- Define `ChunkStatus(str, Enum)`: `PENDING | EMITTED | FAILED | NETWORK_SKIPPED | NETWORK_INCOMPLETE`. String-enum for log readability.
- Change `_chunk_results: dict[int, bool]` (line 131) → `dict[int, ChunkStatus]`. Initialise empty as today.
- **Declare staging + network state in `__init__` (sequenced first so U4 and U5 have load-bearing fields to write to):**
  - `self._chunk_results: dict[int, ChunkStatus] = {}` (migration of line 131)
  - `self._pending_network_status: dict[int, ChunkStatus] = {}` — staging dict where U4 writes `NETWORK_SKIPPED` and U5 writes `NETWORK_INCOMPLETE`; read by `_process_chunk`'s final-assignment step (below) and never observed externally. Lifecycle: cleared per chunk completion (`pop(idx, None)` after final assignment) so a long-running recording does not grow the dict unbounded.
  - `self._recording_id: int` — populated at `__init__` via a one-shot SQLite query against `recording.db` (raw sqlite3 via `open_recording_db`, single seam). Required by U4's `NetworkScrubPipeline(db_path, recording_id)` construction and the network-row + NetworkHealth time-range queries in U4 / U5.
  - `self._network_scrub_pipeline: NetworkScrubPipeline | None = None` and `self._network_scrub_attempted: bool = False` — populated lazily by U4 on first body-bearing chunk.
- **PENDING initialisation contract:** when `_run` receives a rotation message at line 289–296, before `_process_chunk(msg)` runs, set `self._chunk_results[idx] = ChunkStatus.PENDING`. This is the survivorship-bias fix per the prior-incident doc — force-stop now leaves `PENDING` entries that the gate predicate explicitly rejects.
- Rewrite `all_chunks_uploaded()` (line 153) to compare `all(s == ChunkStatus.EMITTED for s in self._chunk_results.values())`. Documented comment cites the upstream incident.
- Rewrite `upload_summary()` (line 163): `n_emitted = sum(1 for s in self._chunk_results.values() if s == ChunkStatus.EMITTED)`; `n_total = len(self._chunk_results)`. Return type unchanged.
- Rewrite `reconcile_against_gcs()` (line 173–230): iterate over chunks where `s == ChunkStatus.FAILED` ONLY (NOT `s != ChunkStatus.EMITTED` — `NETWORK_SKIPPED` and `NETWORK_INCOMPLETE` are terminal-non-EMITTED states that reconcile MUST NOT touch). On reconcile success for a `FAILED` chunk, flip to `EMITTED` (was `True`). **Why this matters:** for a `NETWORK_SKIPPED` chunk, the *core* files (video/audio/events.jsonl/manifest) did upload successfully — only the network bodies are absent. A `!= EMITTED` iteration would call `request_signed_urls`, GCS would return `url=None` for every core file, the predicate at line 226 would pass, and the chunk would silently relabel `EMITTED` — erasing the network-skip signal. Same shape as `chunk-upload-sentinel-gating-and-data-loss.md` Bug 4 routed through reconcile. Document this explicitly in code comments.
- Rewrite `_delete_old_chunks()` (line 690–709): only delete when `self._chunk_results.get(old_idx) == ChunkStatus.EMITTED`. Critical: `NETWORK_SKIPPED` and `NETWORK_INCOMPLETE` chunks must NOT be auto-deleted — they are local-only and a future re-export may need them.
- Rewrite `_process_chunk` final-assignment so the final-assignment block is reachable from every exit path. Use a `try/finally`-shaped seam — the success path AND the early-return paths (six `_stop_event` checks at lines 314/320/326/330/354 and the `_generate_manifest` re-raise at line 343-348) all run through it. Sketch:

  ```
  success = False
  try:
      ... existing _process_chunk body ...
      success = self._upload_chunk(...) if self._upload_enabled else (self._upload_disabled_reason is None)
  finally:
      pending = self._pending_network_status.pop(idx, None)
      if pending is not None:
          # U4 staged NETWORK_SKIPPED, or U5 staged NETWORK_INCOMPLETE
          # (precedence enforced by U5's unconditional overwrite). Preserved
          # over EMITTED to keep the network signal visible at the gate.
          self._chunk_results[idx] = pending
      elif success:
          self._chunk_results[idx] = ChunkStatus.EMITTED
      else:
          self._chunk_results[idx] = ChunkStatus.FAILED
  ```

  The `pop` clears the staging entry on assignment so the dict does not grow unbounded across a long recording. Every exit path lands the same way — stop-event early returns, manifest re-raise, and the normal success path all end in the `finally` block with a coherent final status.

- Rewrite `_run`'s outer exception handler (line 296) so it ALSO consults `_pending_network_status` before falling back to FAILED — protects the case where `_process_chunk` raises out from under U4/U5's staging write. Sketch:

  ```
  except Exception:
      idx = msg.get("completed_index", -1)
      logger.exception(f"Chunk {idx} processing failed")
      pending = self._pending_network_status.pop(idx, None)
      self._chunk_results[idx] = pending if pending is not None else ChunkStatus.FAILED
  ```

  A NETWORK_INCOMPLETE chunk that hit a transient manifest error now becomes NETWORK_INCOMPLETE (visible at the gate) rather than FAILED (which would lose the network signal).
- `engine/collaborators.py:514-516` sentinel-upload gate already calls `cp.all_chunks_uploaded()` — no change there (the predicate moved inside the helper). Verify the call site reads cleanly.
- `engine/collaborators.py:568-578` stub_recording gate is the same shape.
- Update the test fixtures in `test_chunk_processor.py`: 13 direct `cp._chunk_results[i] = True/False` writes become `= ChunkStatus.EMITTED/FAILED`; 5 read assertions switch from `is True/False` to `== ChunkStatus.EMITTED/FAILED`. The `reconcile_against_gcs` tests at `:1440-1574` need a sweep.

**Patterns to follow:** the `_upload_disabled_reason` sentinel pattern in `chunk_processor.py:121-122` — the migration must not collapse the "disabled ≠ succeeded" distinction it established.

**Execution note:** Begin with a failing test that asserts `all_chunks_uploaded() == False` when `_chunk_results = {0: ChunkStatus.NETWORK_SKIPPED}` — the regression test for fix #4 in the prior-incident doc. The migration is then a sequence of mechanical replacements with that test as the guarantee.

**Test scenarios:**

- Covers AE1: `test_pending_on_rotation_blocks_sentinel`: insert rotation → `_chunk_results[0] = PENDING` → `all_chunks_uploaded() == False` → sentinel gate blocks upload.
- `test_force_stop_leaves_pending_entry`: simulate `_stop_event.set()` during `_process_chunk` → `_chunk_results[idx]` is `PENDING` (not missing — survivorship-bias fix).
- `test_network_skipped_blocks_sentinel`: `_chunk_results = {0: EMITTED, 1: NETWORK_SKIPPED}` → `all_chunks_uploaded() == False`.
- `test_network_incomplete_blocks_sentinel`: same shape with `NETWORK_INCOMPLETE`.
- `test_delete_old_chunks_skips_network_skipped`: `_chunk_results = {0: NETWORK_SKIPPED, 1: EMITTED, 2: EMITTED}`, call `_delete_old_chunks(2, keep_recent=2)` → chunk 0 files remain on disk.
- `test_delete_old_chunks_skips_network_incomplete`: same shape with `NETWORK_INCOMPLETE`.
- `test_failed_chunk_blocks_sentinel`: regression for fix #1 in prior-incident doc — `_chunk_results = {0: FAILED}` → gate blocks.
- `test_reconcile_flips_failed_to_emitted`: `_chunk_results = {0: FAILED}`, mock signed-URL response → `_chunk_results[0] == EMITTED`.
- `test_reconcile_does_not_flip_network_skipped`: explicit — `NETWORK_SKIPPED` is terminal-non-EMITTED; even though core files are on GCS, reconcile MUST NOT touch this state. Verifies the `s == FAILED` iteration restriction.
- `test_reconcile_does_not_flip_network_incomplete`: same shape with `NETWORK_INCOMPLETE`. Both terminal-non-EMITTED states must survive reconcile unchanged.
- `test_pending_status_promoted_to_status_on_manifest_failure`: simulate `_generate_manifest` re-raise with `_pending_network_status[idx] = NETWORK_INCOMPLETE` already staged → final status is `NETWORK_INCOMPLETE` (NOT `FAILED`). Verifies outer-handler staging consult.
- `test_pending_status_cleared_on_stop_event_early_return`: simulate `_stop_event.set()` during scrubbing with pending NETWORK_SKIPPED staged → final status is `NETWORK_SKIPPED`, `_pending_network_status` is empty (popped in `finally`).
- `test_upload_summary_counts_only_emitted`: `_chunk_results = {0: EMITTED, 1: NETWORK_SKIPPED, 2: FAILED}` → `upload_summary() == (1, 3)`.
- `test_was_force_stopped_dominates_status_map`: even when all `_chunk_results` are `EMITTED`, if `_stop_event.is_set()` → gate `False` (the existing dominance rule, restated for the enum).
- Edge case: empty `_chunk_results` (no rotations arrived at all) → `all_chunks_uploaded() == False`. Existing test must pass unchanged.

**Verification:** `pytest tests/test_chunk_processor.py -v` green. Static check: `grep -rn '_chunk_results' src/ tests/` returns no remaining boolean comparisons. Manual walkthrough of `chunk-upload-sentinel-gating-and-data-loss.md` Bugs 1–4 against the new code — each fix mechanism remains intact.

### U4. chunk_processor `_export_events` network integration

**Goal:** Make chunk export produce JSONL files that include `network.*` event lines, scrubbed and interleaved with action/window events.

**Requirements:** Linear "`chunk_processor._export_events()` extension" requirement; "interleave with action/window events sorted by `timestamp_ns`" requirement; "fail-soft on KEK missing" requirement.

**Dependencies:** U2 (`NetworkExportMode`), U3 (`ChunkStatus`).

**Files:**

- `src/screencap/chunk_processor.py:534-590` — `_export_events` rewrite + pipeline-caching state on `__init__`
- `src/screencap/export.py:30-86` — no change needed (already accepts `network_rows`, `network_scrub_pipeline`)
- `tests/test_chunk_processor.py:614-697` — invert `test_v1_scope_guard_no_network_lines_in_chunk_jsonl` into the V1.75 positive control

**Approach:**

- U3 already declares `self._network_scrub_pipeline`, `self._network_scrub_attempted`, and `self._recording_id` in `__init__`. U4 only populates them lazily during `_export_events` and writes to `self._pending_network_status[idx]` (never directly to `_chunk_results`).
- Pre-fetch network rows for the chunk's `[start_ts, end_ts)` window using raw `sqlite3` via `open_recording_db` — matches `export.py` style. Fetch into a Python list via `cur.fetchall()` inside the `with open_recording_db(...)` block and exit the block BEFORE running the scrub pipeline, so the read connection does not hold a lock while the scrubber runs (avoids contention with the concurrent `network_event_writer` mp.Process — see R9). Query: `SELECT * FROM network_event WHERE recording_id = ? AND timestamp_ns >= ? AND timestamp_ns < ? ORDER BY timestamp_ns`. Use `int(start_ts * 1_000_000_000)` and equivalent for `end_ts` to convert seconds → ns at the query boundary. Handle `OperationalError: no such table` (pre-network DB) by returning empty list.
- If rows are empty, fall through to existing behaviour (no network events for this chunk) — pipeline stays None, no KEK prompt. Status outcome: subject to U5's overlap check; defaults to `EMITTED` via U3's final-assignment block.
- **NETWORK_SKIPPED propagation across chunks:** add a sticky flag `self._network_skip_recording: bool = False` to `__init__` (this is the only additional field beyond U3's set). When pipeline construction fails (KEK missing, integrity failure, or frozen-binary trap), set the flag to `True`. On every subsequent `_export_events` call, **before** the row-fetch, check the flag — if set, stage `NETWORK_SKIPPED` for this chunk even when rows are empty, so the recording's network-leg failure signal survives across chunks that happen to have no traffic.
- If rows are non-empty AND `not self._network_scrub_attempted`:
  - Set `self._network_scrub_attempted = True` (one attempt per processor lifetime).
  - If `recording_has_encrypted_bodies(db_path, recording_id)` is False → V1-vintage row in DB? Treat as integrity issue, log warning, set `self._network_scrub_pipeline = None` and `self._network_skip_recording = True`.
  - Otherwise wrap `NetworkScrubPipeline(db_path, recording_id)` in **narrow** try/except scope — see "Exception scope" below.
- Call `export_chunk_events(..., network_rows=rows, network_scrub_pipeline=self._network_scrub_pipeline)`. The downstream `unified_export_events` already handles the no-pipeline case (V1 metadata-only path) and the with-pipeline case (decrypt + scrub).
- **Status routing** (writes to staging, not `_chunk_results`):
  - If `self._network_skip_recording` OR (rows present AND pipeline is None) → `self._pending_network_status.setdefault(idx, ChunkStatus.NETWORK_SKIPPED)`. **`setdefault` not unconditional** — preserves U5's later `NETWORK_INCOMPLETE` overwrite. See U5 for the precedence contract.

- **Exception scope at `NetworkScrubPipeline(...)` construction:**

  ```
  try:
      self._network_scrub_pipeline = NetworkScrubPipeline(db_path, recording_id)
  except KekUnavailableError as exc:
      insert_network_health(event="kek_unavailable", timestamp_ns=time.time_ns(), details=str(exc))
      self._network_scrub_pipeline = None
      self._network_skip_recording = True
  except SystemExit as exc:
      # PyInstaller frozen-binary trap: Presidio/spaCy autodownload can SystemExit(2)
      # from inside ML model loading. Documented in
      # docs/solutions/build-errors/pyinstaller-frozen-binary-ci-failures.md.
      logger.exception("NetworkScrubPipeline init raised SystemExit (frozen-binary trap)")
      insert_network_health(event="kek_unavailable", timestamp_ns=time.time_ns(), details=f"SystemExit: {exc}")
      self._network_scrub_pipeline = None
      self._network_skip_recording = True
  # KeyboardInterrupt and bare BaseException propagate — process-lifecycle signals
  # must not be swallowed by the fail-soft path; a SIGINT mid-export should abort
  # the recorder cleanly, not silently mark all subsequent chunks NETWORK_SKIPPED.
  ```

  This replaces the prior `except BaseException` blanket catch. The frozen-binary case is specifically `SystemExit`; everything outside that named set propagates.

- **DEK zeroization on stop:** add a one-line cleanup in `ChunkProcessor.stop()` (after `_thread.join`): `self._network_scrub_pipeline = None`. Drops the binding so the cached `_dek: bytes` becomes eligible for GC the moment the recording ends, rather than persisting until process death. Document the residual caveat per `crypto.py` ("Python does not guarantee zeroing; process death remains the real cleanup boundary").

- **JSONL file mode:** ensure `write_events_jsonl` (`exporter.py:171`) opens the `.tmp` with mode `0o600` so scrubbed plaintext is not readable by other same-machine users between write and upload. Use `os.open`-based opener:

  ```python
  def _mode_0600_opener(path, flags):
      return os.open(path, flags, 0o600)

  with open(tmp_path, "w", opener=_mode_0600_opener) as f:
      ...
  ```

  POSIX `rename` preserves mode; the final renamed file inherits `0o600`. Same fix to the `.tmp` atomic-write path in `exporter.py:export_recording`.

**Patterns to follow:** raw sqlite queries via `open_recording_db` in `src/screencap/export.py:_fetch_action_rows`. The existing CLI export path in `cli.py:1538-1599` for the `recording_has_encrypted_bodies` → `NetworkScrubPipeline` construction sequence.

**Execution note:** Start with the V1.75 positive-control test (inserts a network row, runs `_export_events`, asserts the chunk JSONL contains a scrubbed `body_text` line). Invert the V1 scope-guard.

**Technical design — chunk export flow:**

```
_export_events(idx, start_ts, end_ts):
    # Read into a list, then close the connection BEFORE running the scrubber
    # (R9 contention: writer process must not block on a read transaction).
    with open_recording_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM network_event WHERE recording_id = ? "
            "AND timestamp_ns >= ? AND timestamp_ns < ? ORDER BY timestamp_ns",
            (self._recording_id, int(start_ts * 1e9), int(end_ts * 1e9)),
        ).fetchall()

    if rows and not self._network_scrub_attempted:
        self._network_scrub_attempted = True
        if recording_has_encrypted_bodies(db_path, self._recording_id):
            try:
                self._network_scrub_pipeline = NetworkScrubPipeline(db_path, self._recording_id)
            except KekUnavailableError as exc:
                insert_network_health(event="kek_unavailable", ts_ns=time.time_ns(), details=str(exc))
                self._network_skip_recording = True
            except SystemExit as exc:                          # frozen-binary trap only
                logger.exception("NetworkScrubPipeline init SystemExit")
                insert_network_health(event="kek_unavailable", ts_ns=time.time_ns(), details=f"SystemExit: {exc}")
                self._network_skip_recording = True
            # KeyboardInterrupt and bare BaseException propagate

    # NETWORK_SKIPPED sticky across chunks: once set, every subsequent chunk
    # carries the signal even if it has zero network rows.
    if self._network_skip_recording or (rows and self._network_scrub_pipeline is None):
        self._pending_network_status.setdefault(idx, ChunkStatus.NETWORK_SKIPPED)
        # setdefault (not =): preserves U5's later unconditional NETWORK_INCOMPLETE.

    events = export_chunk_events(
        ..., network_rows=rows,
        network_scrub_pipeline=self._network_scrub_pipeline,
    )
    write_events_jsonl(jsonl_path, events, meta)   # uses 0o600 opener (see Approach)
```

Directional only. The pending-status routing into U3's `_process_chunk` final-assignment (try/finally seam) is the load-bearing handoff.

**Test scenarios:**

- Covers AE2 (V1.75 happy path): `test_chunk_export_includes_scrubbed_network_lines`: insert encrypted network row + meta row; **mock `screencap.network.crypto.get_kek` / `unwrap_dek` (no real Keychain access — follow the pattern at `tests/test_recorder.py:879`)** → `_export_events` produces JSONL with `event_type: network.request` line, `body_text` is scrubbed (PII replaced), `body_ciphertext` field absent.
- `test_chunk_export_metadata_only_when_kek_missing`: insert encrypted row but mock `crypto.get_kek` to return `None` → `_export_events` produces JSONL with `network.request` line, `body_text` is null, `NetworkHealth(event="kek_unavailable")` row was inserted, chunk status becomes `NETWORK_SKIPPED`.
- `test_pipeline_constructed_once_across_chunks`: process 3 chunks each with network rows; assert `NetworkScrubPipeline.__init__` is called exactly once (mock + side_effect counter). KEK unwrap once is load-bearing per the V1.5 contract.
- `test_pipeline_attempted_only_once_on_kek_failure`: chunk 0 has rows + KEK is missing → pipeline construction is attempted, then `_network_scrub_attempted = True`. Chunk 1 has rows → pipeline NOT re-attempted (no second `NetworkHealth(kek_unavailable)` row).
- `test_no_network_rows_no_pipeline_construction`: recording without `--network` → zero rows → pipeline construction is never attempted (no Keychain prompt).
- `test_no_wrapped_dek_no_pipeline_construction`: recording with network rows but no `network_event_meta` row (V1-vintage anomaly) → no pipeline, status `NETWORK_SKIPPED`, `NetworkHealth` warning row inserted.
- `test_systemexit_in_pipeline_init_caught_fail_soft`: simulate frozen-binary subprocess crash on `NetworkScrubPipeline()` (raise `SystemExit(2)`) → caught, NETWORK_SKIPPED, no crash escapes, NetworkHealth(kek_unavailable, details="SystemExit: 2") row written.
- `test_keyboardinterrupt_in_pipeline_init_propagates`: simulate Ctrl+C arriving during `NetworkScrubPipeline()` (`KeyboardInterrupt`) → propagates out of `_export_events`, NOT swallowed, no NetworkHealth row written. Regression guard against the prior overly-broad `except BaseException` catch.
- `test_network_skip_propagates_to_chunks_without_rows`: chunk 0 has rows + KEK missing → NETWORK_SKIPPED. Chunk 1 has zero network rows → still NETWORK_SKIPPED (sticky via `_network_skip_recording`). Without this, the recording-level failure signal evaporates the moment a chunk happens to have no traffic.
- `test_jsonl_file_has_mode_0600`: `_export_events` produces `events_NNNN.jsonl` → `os.stat().st_mode & 0o777 == 0o600`. Regression guard for pre-upload local-file exposure.
- `test_dek_zeroized_on_chunk_processor_stop`: process one body-bearing chunk (pipeline cached with DEK), call `ChunkProcessor.stop()` → `self._network_scrub_pipeline is None`. Caveat: Python does not guarantee actual memory zeroing; this verifies the binding is dropped, which is the strongest contract Python can give.
- `test_chunk_jsonl_excludes_ciphertext_fields`: even on the metadata-only path (`network_scrub_pipeline=None`), inserted rows are emitted into the JSONL but the export-side classes do NOT carry `body_ciphertext` / `body_nonce` / `body_aad` columns — this filtering already lives in `screencap.engine.convert.dict_to_network_event` (V1.5 surface), so the test only verifies that the V1.75 plumbing does not accidentally bypass the converter. Scope guard against ciphertext leakage.
- `test_network_rows_in_chunk_window_only`: insert events spanning two chunks; export chunk 0 → contains only chunk-0 events; export chunk 1 → contains only chunk-1 events. Boundary by `timestamp_ns >= start_ns AND < end_ns`.
- `test_pre_network_db_returns_empty_rows`: open a recording DB without the `network_event` table → query handles `OperationalError: no such table` and returns []. Chunk export succeeds with no network lines.

**Verification:** `pytest tests/test_chunk_processor.py::TestUnifiedEventExport -v`. Chunk JSONL produced by `_export_events` contains both `event_type: window.switch` and `event_type: network.request` lines in `timestamp_ns` order.

### U5. NetworkHealth overlap → `NETWORK_INCOMPLETE`

**Goal:** Mark chunks whose time window overlaps a `proxy_crashed` or `network_writer_failed` health event as `NETWORK_INCOMPLETE`.

**Requirements:** Linear "Query `NetworkHealth` rows ... for each chunk's time range, mark chunks overlapping a `proxy_crashed`/`network_writer_failed` row as `NETWORK_INCOMPLETE`" requirement.

**Dependencies:** U1, U3, U4.

**Files:**

- `src/screencap/chunk_processor.py` — overlap query in `_export_events`; status routing
- `tests/test_chunk_processor.py` — overlap coverage

**Approach:**

- After `export_chunk_events` completes and before the final status is assigned, query `NetworkHealth` for rows where `recording_id = ?` AND `event IN ('proxy_crashed', 'network_writer_failed')` AND `timestamp_ns BETWEEN start_ns AND end_ns`. Use raw sqlite via `open_recording_db` (same `with ... fetchall()` close-before-scrub pattern as U4's row fetch). If any row exists, **unconditionally** assign `self._pending_network_status[idx] = ChunkStatus.NETWORK_INCOMPLETE` — overwriting any prior NETWORK_SKIPPED stage from U4. This makes the precedence rule (NETWORK_INCOMPLETE > NETWORK_SKIPPED) explicit in code rather than implicit via call order: U4 uses `setdefault` (writes only if unset), U5 uses `=` (unconditional overwrite). Survives refactors that reorder U4 and U5.
- `proxy_started` and `kek_unavailable` events do NOT trigger `NETWORK_INCOMPLETE` — `proxy_started` is informational; `kek_unavailable` is already handled by U4's `NETWORK_SKIPPED` path.
- The pending-status dict is consumed at the end of `_process_chunk` (U3's `try/finally` final-assignment): if `idx in self._pending_network_status`, the popped value wins; otherwise the final status falls back to `EMITTED` on upload success or `FAILED` on upload failure. The outer `_run` exception handler also consults the staging dict so a manifest-raise doesn't downgrade NETWORK_INCOMPLETE to FAILED.

**Patterns to follow:** raw sqlite queries via `open_recording_db` in `_fetch_action_rows`.

**Test scenarios:**

- Covers AE3: `test_chunk_overlapping_proxy_crashed_marked_incomplete`: insert `NetworkHealth(event="proxy_crashed", timestamp_ns=mid_chunk)` → process chunk → `_chunk_results[idx] == ChunkStatus.NETWORK_INCOMPLETE`.
- `test_chunk_overlapping_network_writer_failed_marked_incomplete`: same shape with `network_writer_failed`.
- `test_chunk_outside_health_window_emitted_normally`: `NetworkHealth(timestamp_ns=outside_any_chunk)` → all chunks `EMITTED`.
- `test_proxy_started_event_does_not_mark_incomplete`: `NetworkHealth(event="proxy_started")` overlap → chunk still `EMITTED` (informational only).
- `test_kek_unavailable_event_does_not_double_mark`: `NetworkHealth(event="kek_unavailable")` overlap → `NETWORK_SKIPPED` (from U4), not double-counted as `NETWORK_INCOMPLETE`.
- `test_incomplete_precedence_over_skipped`: simulate a chunk where both U4's `NETWORK_SKIPPED` would fire AND U5's `NETWORK_INCOMPLETE` overlap exists → final status is `NETWORK_INCOMPLETE`.
- `test_health_overlap_blocks_sentinel`: chunk `NETWORK_INCOMPLETE` → `all_chunks_uploaded() == False` → sentinel not uploaded.

**Verification:** `pytest tests/test_chunk_processor.py -v -k network_incomplete`. End-to-end: simulate proxy crash mid-recording via fixture, verify the affected chunk's status is `NETWORK_INCOMPLETE`.

### U6. NetworkHealth emission at proxy lifecycle hooks

**Goal:** Wire `NetworkHealth` writes at the four emission points so U5 has data to read.

**Requirements:** Linear "written by network reader/writer when failures occur" requirement.

**Dependencies:** U1.

**Files:**

- `src/screencap/engine/recorder.py:2693-2708` (`_setup_network_capture`) — emit `proxy_started` immediately AFTER `proxy_proc.start()` and the `started_event.wait(...)` confirms the proxy is alive. This is where `recording.id` is available (from `crud.insert_recording` at `:1841`, then propagated via `recording` local) and where the proxy mp.Process is truly running. Do **not** emit from `_DefaultPolicy.setup()` (`network_policy.py:105`) — `setup()` returns BEFORE the proxy spawns and has no DB session.
- `src/screencap/engine/recorder.py` around `:3500-3520` — the existing `event_processor` task health-check loop already detects child-process exit and sends a `record.child_died` status-pipe message consumed at `:3872` into `self._child_crashes`. Hook `proxy_crashed` emission into the `child_died` branch when the dead task matches the proxy's process name. Verify whether `proxy_proc` is registered in `task_by_name` (the existing `network_event_writer` task is) — if not, register it as part of this unit (small isolated edit).
- `src/screencap/engine/recorder.py` around `:1064` (or the mp.Queue consumer that calls `crud.insert_network_event`) — wrap the insert in try/except; on exception emit `network_writer_failed` from inside the writer process; continue consuming
- U4 already wires `kek_unavailable` from within `_export_events` — no additional emission needed here
- `tests/test_network_policy.py`, `tests/test_recording_integration.py` — emission coverage

**Approach:**

- Each emission opens a short-lived SQLAlchemy session (`get_session_for_path(db_path)`) → `insert_network_health(session, recording, event=..., timestamp_ns=time.time_ns(), details=...)`. Unbuffered + immediate commit (matching `insert_network_event_meta`).
- **Cross-process engine constraint:** SQLAlchemy engines are not fork-safe. The `network_event_writer` mp.Process must create its **own** engine via `get_session_for_path(db_path)` *after* the spawn — never inherit a parent-side engine across the spawn boundary. Follow the existing `write_network_events` writer-process pattern in `engine/recorder.py:975+` (the per-process `get_session_for_path(db_path)` call lands at `:1020`, after which `crud.insert_network_event` runs against that session at `:1064`). SQLite WAL + `busy_timeout=5000` (`engine/db/__init__.py:59-62`) makes concurrent writes from three writers (parent, writer process, ChunkProcessor thread) safe under this constraint.
- `proxy_started`: emitted from `_setup_network_capture` (`engine/recorder.py:2693-2708`) immediately after `proxy_proc.start()` returns and `started_event.wait(...)` confirms the proxy is responsive. `recording.id` is available in scope; open a short-lived session via `get_session_for_path(db_path)` (parent-process engine; safe — runs once before any cross-process writer needs the DB). `details` is None.
- `proxy_crashed`: piggyback on the existing `record.child_died` status-pipe branch at `engine/recorder.py:3500-3520`. When the dead child is the proxy, write a `proxy_crashed` row with `details = {exit_code, log_tail}` (log_tail pulled from the existing `_emit_log` channel — same source the V1 fatal-emit pattern uses at `network/proxy_runner.py:136-160`). If `proxy_proc` is not yet in `task_by_name`, registering it is part of this unit; that's a small, isolated edit.
- `network_writer_failed`: inside the writer mp.Process, around `crud.insert_network_event` (~`engine/recorder.py:1064`), catch `Exception` per insert. On failure emit `network_writer_failed` (own session, per cross-process engine constraint above) with the exception type + truncated message; continue consuming so the rest of the queue still drains.
- `kek_unavailable`: already emitted in U4 — no additional work here, just verify the flow.
- **Do NOT use the daemon EventBus for these writes.** Per `eventbus-late-listener-replay-2026-05-12.md`, late-listener races can drop early events. Direct DB writes side-step the issue; readers (U5 in chunk_processor) poll the DB at chunk export time.

**Patterns to follow:** `insert_network_event_meta` write site at `engine/recorder.py:1064` for the unbuffered-write idiom.

**Test scenarios:**

- `test_proxy_started_emitted_on_setup_success`: mock `_DefaultPolicy.setup()` happy path → `NetworkHealth(event="proxy_started")` row exists with `timestamp_ns` near now.
- `test_proxy_started_not_emitted_on_setup_failure`: setup raises before proxy lock acquired → no `proxy_started` row (but possibly a `proxy_crashed` row depending on the failure point — covered separately).
- `test_proxy_crashed_emitted_on_unexpected_exit`: kill proxy mp.Process with non-zero exit; observer thread detects exit → `NetworkHealth(event="proxy_crashed", details=<exit_code + log tail>)`.
- `test_proxy_crashed_not_emitted_on_graceful_shutdown`: stop signal sent + clean exit → no `proxy_crashed` row.
- `test_network_writer_failed_on_insert_exception`: mock `crud.insert_network_event` to raise on row 3 → row 3 surfaces a `NetworkHealth(event="network_writer_failed")`, rows 4+ continue processing.
- `test_writer_continues_after_health_write_failure`: even if `insert_network_health` itself raises, the queue consumer does not crash (defense in depth — never let observability writes take down the data path).
- Edge case: emission during readonly DB (race with `Capture.load(readonly=True)`) — `insert_network_health` swallows OperationalError and logs a warning; the recording still works.

**Verification:** `pytest tests/test_network_policy.py tests/test_recording_integration.py -v -k 'network_health or proxy_started or proxy_crashed'`. End-to-end: run a short recording with `--network`, kill the proxy with SIGKILL mid-recording → confirm one `proxy_crashed` row, then verify U5's chunk-overlap test against the affected chunk.

### U7. `timestamp_ns` sort + interleave correctness fix

**Goal:** Stable ordering for high-frequency network events within sub-millisecond windows. Linear specifies `timestamp_ns` ordering for WebSocket bursts within 10 ms windows.

**Requirements:** Linear "interleave with action/window events sorted by `timestamp_ns` (NOT `timestamp` — stable ordering for high-frequency WS bursts within 10ms windows)" requirement.

**Dependencies:** U4 (the sort-key fix is only load-bearing once network rows actually enter the merge). U7 ships in lockstep with U4; without U4 the bug is latent. This dependency keeps a regression in action/window ordering from being mis-attributed to V1.75 if landed standalone.

**Scope note:** U7 modifies the sort key used for **network events** in the interleave step. Action and window event ordering (which already uses float `timestamp` via a separate path) is not touched.

**Files:**

- `src/screencap/engine/export.py:286` — `network_events.sort` key swap
- `src/screencap/engine/processing.py:1089` — `interleave_network_events` comparison key swap
- `tests/test_unified_export_contract.py` — tiebreaker coverage

**Approach:**

- Today: `network_events.sort(key=lambda e: e.timestamp)` — float seconds. The comparison in `interleave_network_events` uses `<=` on `.timestamp`, ties resolved by event-source order.
- Change sort key to **`lambda e: e.timestamp_ns if getattr(e, "timestamp_ns", None) is not None else int(e.timestamp * 1_000_000_000)`** — **explicit None check, not `or`**. The `or` form treats `timestamp_ns == 0` as falsy and falls through to the lossy float-second fallback. `convert.dict_to_network_event` historically coerces missing/NULL columns to `ts_ns = row.get("timestamp_ns") or 0` (`convert.py:315`), so 0 IS a legal stored value that would silently bypass the entire fix. Tighten `convert.py:315` in the same change: preserve `None` instead of coercing to `0`, so downstream None-vs-0 semantics stay unambiguous.
- Update `interleave_network_events` comparison to use the same key. The merge invariant (action-first vs network-second on tie) is preserved by ordering the conditional check first on action `timestamp_ns` vs network `timestamp_ns`.
- Document the change in module docstrings with a one-line "V1.75: ns-precision ordering for WS frame bursts".

**Patterns to follow:** existing sort + merge in the same file — minimal-diff refactor.

**Test scenarios:**

- `test_network_events_sorted_by_timestamp_ns`: two WS frames at `timestamp_ns = 1000` and `1001` (same float second after rounding) → exported in `1000, 1001` order. Today's float-seconds sort would tie-break unstably.
- `test_action_and_network_interleave_by_ns`: action at `timestamp_ns=500`, network at `timestamp_ns=1000`, action at `timestamp_ns=1500` → interleaved sequence is `[action, network, action]`.
- `test_tied_timestamps_action_first`: action and network event with identical `timestamp_ns` → action comes first (existing merge invariant preserved).
- `test_legacy_event_without_timestamp_ns_falls_back`: event with only `timestamp` (float) → sort uses `int(timestamp * 1e9)` fallback, no AttributeError.
- `test_timestamp_ns_zero_uses_zero_not_fallback`: event with explicit `timestamp_ns = 0` and `timestamp = 0.000001` → sorts as 0, not as `1000` (the falsy-fallback bug regression). Without the explicit None check, the `or` form would incorrectly fall back to `int(0.000001 * 1e9) = 1000`.
- `test_convert_preserves_none_timestamp_ns`: `dict_to_network_event` on a row with missing `timestamp_ns` column produces a Pydantic event whose `timestamp_ns is None`, not `0`. Regression guard for the `convert.py:315` tighten.

**Verification:** `pytest tests/test_unified_export_contract.py -v -k 'timestamp_ns or interleave'`.

### U8. Wire `NetworkExportMode` into explicit-export callers

**Goal:** Make the CLI export and `exporter.py` paths explicitly request `REQUIRE_DECRYPT` semantics, so KEK unavailability fails loud where the user can see it.

**Requirements:** Linear "`REQUIRE_DECRYPT` for explicit `screencap export` — fail-loud" requirement.

**Dependencies:** U2.

**Files:**

- `src/screencap/cli.py:1538-1599` — pass `NetworkExportMode.REQUIRE_DECRYPT` on the user-facing `screencap export` path
- `src/screencap/cli.py:1108` (`_auto_export`) — `export_recording` is called WITHOUT `include_network=True` today, so no mode is required. Add a one-line guard comment: `# network export disabled here — if include_network=True is ever added, pass mode=NetworkExportMode.REQUIRE_DECRYPT explicitly`. Prevents a future caller from accidentally inheriting fail-loud semantics in a background path.
- `src/screencap/cli.py:2894` (upload-recovery path) — same shape, same guard comment.
- `src/screencap/exporter.py:41-125` — accept `mode: NetworkExportMode = NetworkExportMode.REQUIRE_DECRYPT` kwarg; emit a structured audit-log line on the REQUIRE_DECRYPT success path (see Audit log below).
- `src/screencap/export.py:30-86` — accept and forward mode kwarg (optional — could be threaded only through `exporter.py` if chunk processor never calls `export_recording`); confirm during implementation.
- `tests/test_cli.py`, `tests/test_export.py` (if exists) — fail-loud + audit-log coverage

**Approach:**

- Add `mode: NetworkExportMode = NetworkExportMode.REQUIRE_DECRYPT` kwarg to `export_recording` in `exporter.py`. The CLI path passes it explicitly; chunk processor does not call this function (it calls `export_chunk_events` directly), so the default value applies to nobody — making it explicit-only is fine.
- The mode parameter is consumed at the `NetworkScrubPipeline(...)` construction site in `cli.py:1568`. Today: `except KekUnavailableError` is caught and returns -1. With explicit `REQUIRE_DECRYPT` mode, the behaviour is unchanged (already fail-loud). The mode parameter exists primarily to make the contract **explicit and self-documenting** — the type signature now communicates "this caller expects decrypt to be possible".
- Add a docstring block on `export_recording` documenting both modes and citing this plan + Linear ticket.

- **Audit log on REQUIRE_DECRYPT success.** Immediately after `NetworkScrubPipeline(...)` constructs successfully on the explicit-export path, append a structured JSON line to `~/.screencap/run/export-audit.log` (file mode `0o600` if newly created — use `os.open` opener as in U4):

  ```python
  audit_path = Path.home() / ".screencap" / "run" / "export-audit.log"
  audit_path.parent.mkdir(parents=True, exist_ok=True)
  with open(audit_path, "a", opener=_mode_0600_opener) as f:
      f.write(json.dumps({
          "recording_id": recording_id,
          "recording_name": recording_name,
          "output_path": str(output_path) if output_path else "stdout",
          "euid": os.geteuid(),
          "timestamp_ns": time.time_ns(),
          "screencap_version": __version__,
      }) + "\n")
  ```

  Why local-side audit matters: Gate 1 sub-decision 5 covers bucket-read audit; nothing today records who locally decrypted a recording. For a tool intercepting auth tokens and form submissions, "who exported X and where" is forensically load-bearing — single `logger`-style append, no new subsystem.

**Patterns to follow:** the existing CLI export flow at `cli.py:1538-1599`.

**Test scenarios:**

- `test_cli_export_fails_loud_on_kek_missing`: invoke `screencap export <recording>` with KEK absent → exit code non-zero, stderr contains "KEK unavailable" guidance, no JSONL produced.
- `test_export_recording_default_mode_is_require_decrypt`: call `export_recording(recording_dir, ..., include_network=True)` with no `mode` kwarg → KEK absent raises `KekUnavailableError`.
- `test_export_recording_metadata_only_mode_returns_partial`: call `export_recording(..., mode=NetworkExportMode.METADATA_ONLY)` with KEK absent → returns event count > 0, network events present in output with `body_text=None`. (Not a current production path, but documents the contract.)
- `test_explicit_export_does_not_silently_skip_bodies`: regression for the prior-incident "no-events looks like success" pattern — fail-loud must produce a visible error, not a JSONL with zero network lines.
- `test_require_decrypt_writes_audit_log_entry`: run `screencap export` with KEK available → `~/.screencap/run/export-audit.log` contains one JSON line with recording_id, recording_name, output_path, euid, timestamp_ns, screencap_version. File mode `0o600`.
- `test_audit_log_file_mode_0600`: stat the audit-log file → mode is `0o600`.

**Verification:** `pytest tests/test_cli.py -v -k 'export'`. Manual: `screencap export <recording>` with the Keychain entry removed surfaces a clear error.

### U9. Frozen-binary smoke check for `NetworkScrubPipeline`

**Goal:** Catch the next PyInstaller regression for `NetworkScrubPipeline` at build time. The known trap pattern (`AnalyzerEngine()` no-arg → spaCy autodownload → `SystemExit(2)` in frozen binary) is documented in `docs/solutions/build-errors/pyinstaller-frozen-binary-ci-failures.md`.

**Requirements:** `docs/solutions/build-errors/pyinstaller-frozen-binary-ci-failures.md` — every `collect_all` should have a corresponding smoke test.

**Dependencies:** U4 (smoke target exercises the cached-pipeline construction U4 introduces).

**Scope note:** This unit ships a smoke check **only**. Inline `AnalyzerEngine` construction guards are gated on a verification step: if the existing privacy scrubber pattern (`privacy/pii.py:130` already passes `nlp_engine=`) is already followed everywhere under `src/screencap/network/`, no source-code fix is needed and the unit collapses to its smoke-test deliverable. If verification surfaces a gap, file a separate build-infrastructure ticket — do NOT couple `pyinstaller/screencap.spec` edits to SCR-12.

**Files:**

- `_smoke-test` (locate during implementation — likely `src/screencap/cli.py:_smoke-test` or a sibling) — add `NetworkScrubPipeline.__init__` invocation against a throwaway DB
- `tests/test_smoke.py` (or equivalent) — frozen-binary smoke coverage

**Approach:**

- **Step 1: Verify (pre-edit).** Run `grep -rn 'AnalyzerEngine(' src/screencap/network/ src/screencap/privacy/`. If every call passes `nlp_engine=` (matching `privacy/pii.py:130`), proceed directly to Step 2 with **no source-code edits**.
  - If the grep surfaces a bare `AnalyzerEngine()` in the network path, **stop and file a separate ticket** — do not patch under SCR-12. The fix is a one-line mirror of the privacy pattern but the cost of conflating it with V1.75 scope is exactly the kind of build-infrastructure scope creep flagged in this plan's review.
- **Step 2: Smoke check.** Add a smoke-test step that instantiates `NetworkScrubPipeline` against a minimal recording DB containing one `network_event_meta` row + mocked `crypto.get_kek` (returning a 32-byte fixture). Assert successful construction (no `SystemExit`, no `ModuleNotFoundError`).

**Patterns to follow:** existing smoke-test entries for the privacy scrubber.

**Test scenarios:**

- `test_smoke_network_scrub_pipeline_constructs_in_frozen_binary`: runs the smoke binary against a throwaway DB, asserts exit 0.
- `test_network_path_has_no_bare_analyzer_engine`: code-search assertion (or static `ast.parse` of `src/screencap/network/`) — fail if `AnalyzerEngine()` appears without `nlp_engine=` kwarg. Regression guard for the trap pattern, regardless of whether the current code currently triggers it.

**Verification:** Run the build pipeline locally (per `release` skill) → smoke test step passes. Manual: install the frozen binary, set up a one-event recording, run `screencap export` against it.

---

## Output Structure

No new directory hierarchy. All changes integrate into existing modules.

---

## Scope Boundaries

### In scope

- All work described in Units U1–U9
- Pre-Implementation Gate 1 (cloud bucket policy brainstorm) as a prerequisite for go-live, not as a planning blocker
- Documentation of the accepted detector-confidence risk in a Scope Boundary note + follow-up ticket reference

### Deferred for later (Linear-acknowledged)

- **DetectionPipeline confidence threshold tuning** — separate ticket, gated on V1.5 false-negative analysis. V1.75 ships with current scrub posture; bodies that pass scrub without entity detection reach the cloud bucket. **Accepted risk** documented here so the trade-off is explicit. Mitigations in place: scrubbed plaintext only (no ciphertext), bucket-side access control (Gate 1), retention policy (Gate 1).
- **`network.session_start` event** — explicitly rejected per Resolved Planning Decisions; can be revisited if a future workflow needs self-describing chunks (decryption outside the recording DB).
- **Cloud Run stitching changes** — V1.75 produces JSONL with `network.*` event types. The Cloud Run side may need updates if it currently rejects unknown event types. Out of scope for this plan; flag via a Linear sub-issue once the JSONL contract is final.

### Deferred to Follow-Up Work (plan-local implementation sequencing)

- **Live-display surfacing of `NETWORK_SKIPPED` / `NETWORK_INCOMPLETE`** — the chunk-progress message could include `(network: SKIPPED)` annotations. Useful but not load-bearing for V1.75; add in a follow-up after U3+U4+U5 are merged.
- **`reconcile_against_gcs` for `NETWORK_SKIPPED` chunks** — today reconcile only flips `FAILED → EMITTED`. A future ticket could re-attempt KEK unwrap if the user added the Keychain entry post-recording. Not needed for V1.75 ship.

### Outside this product's identity

- Plaintext network body upload (no scrub): never. The full premise of V1.5 + V1.75 is scrubbed plaintext only.
- Remote-control of the proxy from the cloud side: never. Local capture + post-scrub upload is the privacy posture.

---

## Risk Analysis & Mitigation

| # | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| R1 | `_chunk_results` reader audit misses a call site → silent regression of fix #1–4 from sentinel-gating doc | Medium | Critical (data loss) | U3 reader list enumerated from `grep -rn '_chunk_results'`. Implementation MUST re-run grep after the migration; any non-`EMITTED` comparison in the result blocks the PR. The internal helpers (`all_chunks_uploaded`, etc.) absorb the predicate so external readers in `engine/collaborators.py` need no awareness of the enum. |
| R2 | KEK Keychain prompt during chunk export interrupts user UX | Low | High (user-visible interruption) | `NetworkScrubPipeline` constructor pulls KEK once per processor lifetime. `recording_has_encrypted_bodies` pre-check avoids the prompt entirely for recordings without encrypted bodies. The prompt only fires once at first body-bearing chunk; if user cancels, `KekUnavailableError` → `NETWORK_SKIPPED` (fail-soft, no further prompts). |
| R3 | `timestamp_ns` sort key change destabilises existing tests | Low | Medium | U7 is isolated to two files. Existing tests assert ordering by event sequence, not by exact timestamp values — should pass through unchanged. Run `pytest tests/test_unified_export_contract.py -v` post-change. |
| R4 | `NetworkHealth` table-creation race with live recording (engine writes rows while `_ensure_network_tables` runs) | Low | Medium | `_ensure_network_tables` is called at `Capture.load()` BEFORE the proxy mp.Process starts. The "live recording" window cannot fire `NetworkHealth` writes before the table exists. Add an explicit assertion in `_ensure_network_tables` test that the table is created before `Capture.load()` returns. |
| R5 | Cloud Run stitching rejects new `network.*` event types in JSONL | Medium | High (uploads succeed but stitching fails) | Gate 1 brainstorm should include a Cloud Run contract verification step. Add a follow-up ticket explicitly: "Verify Cloud Run accepts network.request, network.response, network.ws_upgrade, network.ws_frame event types in event-stitching pipeline." V1.75 ships with chunk JSONL containing the new types; if Cloud Run rejects, fall back to a feature-flag-gated rollout (chunk processor can re-suppress network events behind a config bit until the Cloud Run side updates). |
| R6 | Frozen-binary regression on `NetworkScrubPipeline` model loading | Medium | Critical (silent scrub failure → cloud plaintext leakage) | U9 smoke test. `BaseException` catch in U4. Inline scrubber init in the same PR as the spec-file change. |
| R7 | `_chunk_results` PENDING initialisation race (rotation message arrives, _process_chunk runs before PENDING is written) | Low | Medium | The PENDING write happens in the same `_run` loop iteration as the message receipt, BEFORE `_process_chunk` is called. No race window. Tested in U3's `test_pending_on_rotation_blocks_sentinel`. |
| R8 | NetworkHealth row insertion failure cascades into the network data path (proxy crashes because health-write fails) | Low | High | All `insert_network_health` writes are wrapped in try/except in U6 — observability never takes down the data path. Tested in `test_writer_continues_after_health_write_failure`. |
| R9 | SQLAlchemy engine shared across the mp.Process spawn boundary causes corruption or hangs | Low | Critical | U6 Approach explicitly requires per-process engines (`get_session_for_path(db_path)` called *inside* the writer process after spawn). Follows the existing `insert_network_event_meta` pattern at `engine/recorder.py:1064`. SQLite WAL + `busy_timeout=5000` makes three concurrent writers safe under this constraint. |

---

## Dependencies / Prerequisites

- V1.5 (SCR-11) shipped — confirmed via PR #157 merge
- Gate 1 (cloud bucket policy brainstorm) — required before go-live, not before implementation
- Gate 2 (V1.5 false-negative analysis) — acknowledged and deferred per Scope Boundaries
- No external library upgrades required; V1.75 uses existing `cryptography`, `keyring`, `presidio-analyzer`, `sqlalchemy` versions

---

## Documentation Plan

- Update `CHANGELOG.md` with V1.75 entry mirroring V1.5's structure
- After ship: add a `docs/solutions/` entry for the `_chunk_results → ChunkStatus` migration pattern (compound the learning per existing repo workflow)
- After ship: add a `docs/solutions/` entry for `NetworkHealth`-style observability table pattern (the first migration of its shape in this repo per learnings-researcher finding)
- Inline code comments on the U3 sentinel gate predicate citing `docs/solutions/runtime-errors/chunk-upload-sentinel-gating-and-data-loss.md` (pin-the-rationale pattern from `mitmproxy-ignore-hosts` learning)

---

## Verification Strategy

1. Unit tests for each implementation unit (see per-unit Test scenarios sections)
2. End-to-end integration: `screencap start --network --cloud --chunk-duration 10` for a 30s recording → verify chunk JSONL files contain `network.*` event lines + chunk statuses progress `PENDING → EMITTED` → sentinel uploads
3. Failure injection: kill proxy mid-recording with `SIGKILL` → verify `NetworkHealth(event="proxy_crashed")` row exists + affected chunk(s) marked `NETWORK_INCOMPLETE` + sentinel blocked
4. KEK-removal scenario: start recording, remove Keychain entry mid-recording (or via fixture) → verify `NETWORK_SKIPPED` propagates, chunk JSONL contains metadata-only network lines, recording survives without crash
5. Cloud upload smoke: against a test GCS bucket, verify uploaded JSONL files contain expected `network.*` lines + bucket-level ACL/TLS enforcement is intact (post Gate 1)
6. Frozen-binary smoke (per U9): `python -m build` / PyInstaller spec → smoke test step exercises `NetworkScrubPipeline.__init__`

---

## Deferred / Open Questions

The following findings were surfaced in the 2026-05-14 ce-doc-review and routed to Open Questions because each requires product, threat-model, or cross-team input that this plan does not own. Resolve before V1.75 ships (or document the accepted risk in this section once resolved).

### From 2026-05-14 review

- **[P1] Cloud-side consumer for `network.*` events is never named — value chain unverified.** (product-lens, root of dependency chain) The plan frames V1.75 as "closing the V1.5 plumbing gap" but never names the downstream consumer that needs network events in the cloud. R5 even concedes Cloud Run stitching may not currently accept the new event types. Without a named workflow ("analyst Y queries cloud network logs for Z outcome"), V1.75 risks delivering a producer pipeline whose output sits unconsumed: cost surface, exposure surface, and maintenance surface all grow with no offsetting value. **Resolution requires:** product owner naming the cloud-side consumer + workflow, OR explicit acceptance that V1.75 ships as scope-completion ahead of demand. If neither, consider scoping V1.75 to local-only export until a consumer requirement materialises.

- **[P1] Cloud Run consumer contract not verified before producer-side work commits.** (product-lens, cascades from "Cloud-side consumer never named") R5 acknowledges Medium likelihood that Cloud Run stitching rejects new event types with High impact (uploads succeed silently, stitching fails). Mitigation is a future feature-flag retrofit. The resource-correct sequence is the inverse: verify Cloud Run accepts `network.request` / `network.response` / `network.ws_upgrade` / `network.ws_frame` BEFORE U1–U9 land. **Resolution requires:** inspection of the Cloud Run stitching schema OR a fixture-JSONL upload to staging. Outcome should land either as Pre-Implementation Gate 3 (ship-blocker) or as a confirmed Cloud Run sub-ticket sequenced before U1–U9. Resolves alongside the parent finding above — if V1.75 doesn't ship, no contract to verify.

- **[P1] Gate 1 sub-decisions can constrain U1–U9 design, yet implementation runs in parallel.** (product-lens, cascades from "Cloud-side consumer never named") If retention/audit-log policy requires additional fields in the JSONL (e.g., per-event source-recording identifier, per-chunk provenance token), U4's JSONL schema may need rework after Gate 1 lands. The current "go-live blocker, not planning blocker" framing leaves U1–U9 exposed to schema drift. **Resolution requires:** explicit list of which Gate 1 sub-decisions can possibly add JSONL/health-schema fields, with units gated only on those (rather than the blanket gate). Resolves alongside the parent finding — if V1.75 doesn't ship, no Gate 1 constraint applies.

- **[P1] Gate 1 omits bucket write-access policy — upload credential scope not constrained to write-only.** (security-lens) Gate 1 defines read access but not write. If the upload credential has `storage.objects.list` or `storage.buckets.get` in addition to write, a compromised local process that steals the credential (Keychain, process memory) can enumerate all recordings in the bucket and exfiltrate every scrubbed JSONL. Minimum credential should be `storage.objects.create` scoped to the per-recording prefix `<recording-name>/`, with no list/read. **Resolution requires:** the Gate 1 brainstorm to add this as sub-decision 7 (write-access policy alongside read-access in sub-decision 2).

- **[P2] `NetworkHealth` DB rows have no integrity protection — a tampered DB can lie about `NETWORK_INCOMPLETE` chunks.** (security-lens) The NETWORK_INCOMPLETE logic relies entirely on local SQLite rows. An attacker who can write to `~/.screencap/recordings/<name>/recording.db` can DELETE specific `NetworkHealth` rows to suppress NETWORK_INCOMPLETE marking; the bucket then receives apparently-complete chunks that actually had incomplete proxy coverage. **Resolution requires:** decide whether this threat model (physical access to the recording directory by an attacker who has not already compromised the user account) applies to screencap. If in scope: add HMAC/signature column to `NetworkHealth` or per-recording row-count seal. If out of scope: document the trust boundary in Scope Boundaries / Outside this product's identity.

- **[P2] Presidio / spaCy / GLiNER ML model loading has no supply-chain integrity verification.** (security-lens) `create_default_pipeline()` loads `knowledgator/gliner-pii-base-v1.0` via HuggingFace with no checksum/signature verification. A compromised HF cache or MITM during first download could substitute a model that classifies PII as non-PII — categorically different from the threshold-tuning gap acknowledged in Gate 2. A backdoored model could systematically pass PII for specific patterns while passing all other content correctly, making detection extremely difficult. U9's smoke test verifies construction succeeds but not model identity. **Resolution requires:** decide whether to pin model SHA-256 + verify against HF `model.safetensors` hash at first download, OR document explicitly that the threat model excludes model substitution and rely on macOS Gatekeeper for binary integrity only.
