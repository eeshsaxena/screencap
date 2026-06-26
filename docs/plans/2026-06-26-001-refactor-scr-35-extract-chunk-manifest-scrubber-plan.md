---
title: "refactor: SCR-35 — pull ChunkManifest and ChunkScrubber out of ChunkProcessor"
type: refactor
status: completed
date: 2026-06-26
---

# SCR-35 — Pull ChunkManifest and ChunkScrubber out of ChunkProcessor

## Summary

Extract two narrow seams — `ChunkManifest` (owns "produce a manifest for this chunk", including v1/v2 segmentation-mode selection) and `ChunkScrubber` (owns "scrub this chunk's outputs", consuming the existing `Scrubber`) — out of the `ChunkProcessor` god-object, leaving `ChunkProcessor` a thin sequencer: rotate → manifest → scrub → upload → delete. This is a **behavior-preserving** refactor: no manifest format, scrub behavior, masking policy, upload/delete logic, or data-loss invariant changes. The payoff is locality (manifest-mode decisions in one class) and leverage (each seam becomes a unit-test target that no longer requires a `ChunkProcessor` with a recorder and queues).

---

## Problem Frame

`ChunkProcessor` (`src/screencap/chunk_processor.py`, 1819 lines) owns too much. The "is scrubbing on?" decision is encoded as constructor params plus lazily-`None`-checked fields (`self._pipeline`, `self._anonymizer`, `self._masking_*`), built in a 54-line `_should_init_scrub` block in `__init__` ([chunk_processor.py:178-231](src/screencap/chunk_processor.py)). Manifest-version selection is split across two files: the processor stores `self._segmentation_mode` and passes it down ([chunk_processor.py:165](src/screencap/chunk_processor.py), [:1084](src/screencap/chunk_processor.py)), while `task_manifest.generate_manifest` branches on it internally ([task_manifest.py:45](src/screencap/task_manifest.py)). State leaks across boundaries: a scrub-pipeline init failure under cloud-intent reaches up and mutates the processor's `_upload_enabled` / `_upload_disabled_reason` ([chunk_processor.py:194-199](src/screencap/chunk_processor.py)), entangling the "scrub" concern with the "upload" concern.

Apply the deletion test: each file earns its keep, but the lifecycle is hard to reason about because manifest and scrub responsibilities are interleaved with the safety-critical upload/delete sequencing.

**Honest scope of the payoff:** this refactor retires the *manifest and scrub* contribution to that "hard to reason about" lifecycle. It does **not** touch the other two sources of sequencer complexity — the cross-process flush coordination (`flush_lock` / `flush_requested` / `flush_ack_counter`, shared session → chunk_processor → scrub_worker) and the survivorship-bias status-settling try/finally. Those remain in `ChunkProcessor` by design (the flush seam is deferred to a follow-up; the status-settling is the data-loss-critical code this plan deliberately does not move). So the issue's lifecycle complaint is *partially*, not fully, retired.

**Staleness note:** SCR-35 was filed 2026-05-04 against `chunk_processor.py` at 1265 lines. Since then the SCR-125 unified pipeline landed (`pipeline_stages.py`/`PipelineStageRunner`, `pipeline_state.py`/`PipelineLedger`, `terminal_stage.py`, `retention.py`), and the file *grew* to 1819 lines. This plan is written against the **current** architecture, which materially improves the extraction: manifest production already flows through an injection seam (`PipelineStageRunner` takes an injected `manifest=` step), and per-chunk scrubbing already delegates its step order to `Scrubber.run_chunk()`. The two new classes slot into seams that already exist rather than inventing new ones.

---

## Requirements

- R1. Manifest production for a chunk — v1/v2 mode selection, `blocked_intervals` gathering, and partial-file cleanup-on-failure — is owned by a single `ChunkManifest` seam injected as the `manifest=` step of `PipelineStageRunner`. `ChunkProcessor` no longer reads `self._segmentation_mode` directly.
- R2. Per-chunk scrubbing — the `Scrubber` construction, `run_chunk` call, audit logging, and the "is scrubbing on?" predicate — is owned by a single `ChunkScrubber` seam. The `if self._scrub_enabled and self._pipeline is not None:` None-guard is removed from the sequencer call site.
- R3. The scrub/masking-config initialization (`_should_init_scrub` block) moves into `ChunkScrubber`. The cloud-intent init-failure → upload-disable coupling becomes an **explicit result** the sequencer consumes, not a hidden mutation of `ChunkProcessor` state from inside the scrub concern.
- R4. `ChunkProcessor` reads as a sequencer (rotate → manifest → scrub → upload → delete); manifest and scrub orchestration no longer live inline in it.
- R5. The refactor is behavior-preserving: all five data-loss-prevention invariants and all privacy/masking behavior are unchanged. No edit to `_cloud_upload_chunk`, `_upload_chunk`, `_evict_old_chunks_through_floor`, the `_process_chunk` status-settling try/finally, the ledger mirroring, or the terminal-stage convergence.
- R6. Each seam is independently unit-testable without constructing a `ChunkProcessor` (no recorder, no multiprocessing queues). New `tests/test_chunk_manifest.py` and `tests/test_chunk_scrubber.py` exercise the seams directly.
- R7. The SCR-118 content-index fail-closed guard (`_do_index_chunk_content`, [chunk_processor.py:1168](src/screencap/chunk_processor.py)) continues to gate on a **live** masking-context signal sourced from the `ChunkScrubber` seam — never from a deleted `ChunkProcessor` field. With masking context unavailable, no frames are indexed (the existing fail-closed behavior), so masked-app on-screen text can never leak into the local index. This is the load-bearing dependency that makes "content-index gating unchanged" actually true after the field move (U3).

---

## Scope Boundaries

- **Not** extracting an upload/delete/eviction seam. The issue lists "upload/delete chain" among ChunkProcessor's overreach, but its stated Solution names only two seams (`ChunkManifest`, `ChunkScrubber`), and post-SCR-125 the upload/delete/eviction safety logic already delegates to `terminal_stage.py` / `retention.py` / `PipelineLedger`. Those stay as sequencer methods on `ChunkProcessor`.
- **Not** changing manifest format, scrub step order, masking policy, content-index gating, or any data-loss / privacy logic.
- **Not** touching the cloud window filter built inline in `_export_events` ([chunk_processor.py:1015](src/screencap/chunk_processor.py)). It stays where the static guard `tests/test_privacy_filter_call_graph.py` pins it; the manifest/scrub extraction does not move it.
- **Not** migrating the other `task_manifest.generate_manifest` callers. There are two beyond the live chunk path: `src/screencap/recovery.py` (the resume/recovery path, which re-derives `segmentation_mode` via `get_segmentation_mode()` — a *production* caller) and the config/test callers (`config`, `tests/test_llm_segmentation.py`, `tests/test_recording_integration.py`). `generate_manifest` is retained as-is. Consequence: mode authority is consolidated for the **live chunk path only** (`ChunkProcessor` stops reading `_segmentation_mode`); the recovery path still re-derives the mode independently, so this plan does not fully retire the cross-file mode split — it narrows it to the live path. Unifying live + recovery mode resolution is deferred (see below).

### Deferred to Follow-Up Work

- **Flush-coordination decoupling** (session → chunk_processor → scrub_worker shared `flush_lock`): the issue mentions this in passing as a symptom of the god-object, but it is a distinct seam touching three files and is not one of the two named extractions. Defer to a separate ticket.
- **Unify live + recovery manifest-mode resolution**: route `recovery.py`'s manifest production through `ChunkManifest` (or a shared mode-resolver) so segmentation-mode authority lives in exactly one place across both paths. Out of scope here (the issue names only the chunk-processor extraction); deferred so the "mode locality" benefit can be fully realized later.
- **Documenting the seam-extraction / god-object-decomposition convention** for this codebase via `/ce-compound` once SCR-35 lands (no such doc exists today; flagged by learnings research as a good capture candidate).

---

## Context & Research

### Relevant Code and Patterns

- **`PipelineStageRunner` injection seam** — [src/screencap/pipeline_stages.py](src/screencap/pipeline_stages.py): the runner already takes injected `transcribe` / `export_events` / `manifest` steps and owns only ordering + ledger idempotency. `ChunkProcessor._run_agnostic_stages` ([chunk_processor.py:617-726](src/screencap/chunk_processor.py)) constructs it with closures `_transcribe_step` / `_export_step` / `_manifest_step`. `ChunkManifest.produce` becomes the injected `manifest=` step.
- **`Scrubber` delegation** — [src/screencap/scrubber.py:2102](src/screencap/scrubber.py): the scrubbing-collapse ticket's `Scrubber` exists. `ChunkProcessor._scrub_chunk_files` ([chunk_processor.py:1087-1122](src/screencap/chunk_processor.py)) already constructs a `Scrubber` per chunk and calls `run_chunk()`; the docstring states the processor "only owns lifecycle concerns (which chunks to scrub, when, with what masking config)" — exactly the `ChunkScrubber` responsibility.
- **Scrub-init block** — [chunk_processor.py:178-231](src/screencap/chunk_processor.py): the `_should_init_scrub` decision + pipeline/anonymizer/classifier/evaluator construction + the cloud-intent PUBLIC-mode override + the upload-disable side effect.
- **Sequencer body** — `_process_chunk` ([chunk_processor.py:728-862](src/screencap/chunk_processor.py)): the 7-step sequence with the survivorship-bias try/finally and the unified retention-floor eviction.
- **Content-index fail-closed guard (cross-dependency on the masking fields)** — `_do_index_chunk_content` ([chunk_processor.py:1168](src/screencap/chunk_processor.py)) reads `self._masking_classifier` / `self._masking_evaluator` purely as a fail-closed guard (`if either is None: return` — without them `blocked_intervals` cannot represent masked-app skips, so indexing is refused). This method **stays on `ChunkProcessor`** (it is not one of the two seams), yet U3 deletes the fields it reads — so U3 must re-source this guard from the `ChunkScrubber` seam (R7). The objects are used *only* for the None-guard here; the actual frame-skipping uses `scrub_result.blocked_intervals`, so a boolean `has_masking_context` predicate is sufficient.
- **Second production manifest caller** — `recovery.py` ([recovery.py:185-193](src/screencap/recovery.py)) independently imports `get_segmentation_mode`, derives `seg_mode`, and calls `task_manifest.generate_manifest(..., segmentation_mode=seg_mode)` on the resume/recovery path. This is the *only non-test* `generate_manifest` caller besides the live chunk path, and it bounds how much "mode locality" Approach A can deliver (see Key Technical Decisions).
- **Prior architecture plan** — [docs/plans/2026-06-09-001-refactor-scr-125-unified-per-chunk-upload-plan.md](docs/plans/2026-06-09-001-refactor-scr-125-unified-per-chunk-upload-plan.md): the SCR-125 refactor whose seams this plan extends. Mirror its U-ID/R-ID template and its "behavior-preserving, invariant-first" posture.
- **Tests** — `tests/test_chunk_processor.py` (2649 lines, also covers manifest + data-loss invariants), `tests/test_scrubber_class.py` (1012), `tests/test_pipeline_stages.py` (259), `tests/test_terminal_stage.py` (1369), `tests/test_llm_segmentation.py` (v1/v2 manifest).

### Institutional Learnings

- **`docs/solutions/runtime-errors/chunk-upload-sentinel-gating-and-data-loss.md`** — the five data-loss-prevention invariants that any refactor of `_process_chunk`'s status-settling / eviction logic must NOT break: (1) closed-set chunk tracking seeded at rotation, (2) tri-state upload state (`UPLOADED ≠ SKIPPED ≠ FAILED`, "disabled" ≠ "succeeded"), (3) `was_force_stopped` forces the completeness signal `False`, (4) sentinel is the last write gated on all prior writes, (5) constructor invariant `_auto_delete=False` whenever `_upload_enabled=False`. **Post-SCR-125 these now live primarily in `PipelineLedger` / `terminal_stage.py` / `retention.py`, not `chunk_processor.py`** — the thin sequencer must keep delegating to those owners; the new seams must not re-own status-settling. This plan does not edit any of that logic (R5).
- **`docs/solutions/integration-issues/inner-timeout-unreachable-behind-outer-watchdog-2026-06-24.md`** (SCR-175) — `run_terminal_stage` fires an `on_progress(phase)` heartbeat (`"locked"` / `"reconcile"` / `"scrub"`) so the interactive upload watchdog sees progress during long silent scrub prep. This plan does not relocate the terminal-stage `produce()` call, so the heartbeat is untouched; verification confirms it.
- **`docs/solutions/workflow-issues/privacy-guards-deselected-on-ci-silently-rot-2026-06-10.md`** — background-window masking guards are `@pytest.mark.privacy`. **Correction to an earlier assumption: SCR-133 (2026-06-17) closed the CI blind spot** (doc line 131) by adding two lanes that DO run the guards on every PR: `privacy-guards-macos` (Vision installed) and `privacy-guards-visionfree` (Ubuntu, fake OCR stack forcing the masking path). So green CI now *does* exercise masking — the done-criterion is confirming **both SCR-133 lanes still pass** after the seam move (a local Vision-Mac run is a belt-and-suspenders supplement, not the only signal). The doc also records the SCR-30→SCR-110 leak: a boolean that coupled two disjoint scrub operations caused a privacy leak. Lesson for this extraction: introduce **no new coupling booleans**; move the existing config verbatim.

### External References

- None required. This is an internal mechanical refactor against well-established local patterns (3+ direct examples of the injection-seam and per-chunk-delegation patterns already exist in the repo).

---

## Key Technical Decisions

- **ChunkManifest owns the mode; `task_manifest` stays the format renderer (approach A, recommended).** `ChunkManifest` holds `segmentation_mode`, gathers `blocked_intervals` from the screen filter, owns partial-file cleanup, and calls `task_manifest.generate_manifest(..., segmentation_mode=self._mode)`. The v1/v2 *render* branch stays inside `generate_manifest` (it is the format renderer's internal detail), but the **live chunk pipeline's** *decision authority* over which mode this recording uses now lives in one class — `chunk_processor` stops reading `_segmentation_mode`. Rationale: narrowest change, zero dispatch duplication, and `generate_manifest`'s other callers are untouched. **Scope caveat (honest locality):** `recovery.py:189-193` independently re-derives the mode and calls `generate_manifest` on the resume path, so this does not collapse mode selection to a single owner across the *whole* system — only across the live path. Fully unifying live + recovery is deferred (Scope Boundaries). (Alternative B — absorbing the dispatch branch into `ChunkManifest` and reducing `generate_manifest` to a shim — is heavier and still wouldn't capture the recovery path; see Alternatives Considered.)
- **The scrub-init → upload-disable coupling becomes an explicit return value.** Today, scrub-pipeline init failure under cloud-intent mutates `ChunkProcessor._upload_enabled` / `_upload_disabled_reason` from inside the init block. `ChunkScrubber` instead returns/exposes an init result (e.g. `ScrubInit{enabled: bool, disable_uploads_reason: str | None}`); the sequencer reads it and applies the upload-disable + the `_auto_delete=False` safety invariant itself. The scrub concern reports a fact; the sequencer owns the upload policy. This is the central "stop the state leak" move (R3).
- **`ChunkScrubber` owns the "is scrubbing on?" predicate.** The `_scrub_enabled and _pipeline is not None` guard moves inside the seam as `is_enabled` (or a `scrub()` that returns `None` when off). The sequencer asks the seam, never inspects scrub internals.
- **Both seams are plain constructor-injected collaborators, not lazily-`None` fields.** `ChunkProcessor.__init__` builds a `ChunkManifest` and a `ChunkScrubber` and holds them as `self._chunk_manifest` / `self._chunk_scrubber`. No behavior depends on import order; heavy imports (`redaction`, `privacy.classify/policy`) move into `ChunkScrubber` and stay deferred exactly as today.
- **Behavior-preserving, invariant-first.** No edit to the upload/delete/eviction/terminal-stage/ledger code. The `tests/test_chunk_processor.py` **data-loss invariant** tests are the runtime regression oracle and must stay green (R5). The scrub/manifest-internals tests in that file (which poke now-deleted private fields) are migrated to the seam test files in U1–U3 — "behavior-preserving" is a claim about runtime behavior, not about tests coupled to removed internals.

---

## High-Level Technical Design

> *This illustrates the intended approach and is directional guidance for review, not implementation specification. The implementing agent should treat it as context, not code to reproduce.*

**Seam ownership (after):**

| Responsibility | Today (in `ChunkProcessor`) | After |
|---|---|---|
| Pick segmentation mode (v1 idle / v2 llm) | `self._segmentation_mode` + passed to `generate_manifest` | `ChunkManifest` (holds mode) |
| Gather `blocked_intervals` from screen filter | `_manifest_step` closure | `ChunkManifest.produce` |
| Partial-manifest cleanup on failure | `_manifest_step` closure | `ChunkManifest.produce` |
| "Is scrubbing on?" predicate | `_scrub_enabled and _pipeline is not None` at call site | `ChunkScrubber.is_enabled` |
| Build scrub pipeline / anonymizer / masking config | `__init__` `_should_init_scrub` block | `ChunkScrubber` constructor |
| Cloud-intent → PUBLIC masking mode | `__init__` | `ChunkScrubber` constructor |
| Per-chunk `Scrubber` construction + `run_chunk` + audit log | `_scrub_chunk_files` | `ChunkScrubber.scrub` |
| Init-failure → disable uploads | direct mutation of `_upload_enabled` | explicit `ScrubInit` result consumed by sequencer |
| Content-index fail-closed masking guard | reads `self._masking_classifier`/`_evaluator` | reads `ChunkScrubber.has_masking_context` (seam predicate); method stays on `ChunkProcessor` |
| Wait-audio, status-settling, upload, eviction, ledger mirror | `_process_chunk` / upload+delete methods | **unchanged** — stays in `ChunkProcessor` |

**Sequencer shape (after) — `_process_chunk` reads as:**

```
wait_for_audio(idx)
artifacts = run_agnostic_stages(idx, …)        # injects chunk_manifest.produce as manifest step
scrub_result = chunk_scrubber.scrub(idx, …)    # None when scrubbing off; no inline None-guard
if scrub_result is not None:
    index_chunk_content(idx, …, scrub_result)  # SCR-118; its fail-closed
    # masking-context guard now reads chunk_scrubber.has_masking_context
cloud_upload_chunk(idx, …)                     # unchanged: terminal flock, ledger mirror
evict_old_chunks_through_floor(idx)            # unchanged: retention floor
```

**Scrub-init decision matrix (must be preserved exactly — this is the subtle part):**

| cloud_intent | upload_enabled | deps present | Outcome |
|---|---|---|---|
| true | true | yes | scrubbing on, masking mode = PUBLIC |
| true | true | **no** | uploads disabled w/ reason, `_auto_delete→False` (consumed from `ScrubInit`) |
| false | — | yes (opt-in) | scrubbing on, masking mode = config default |
| false | — | **no** (opt-in) | scrubbing silently off (`is_enabled=False`), uploads NOT disabled |
| false | — | — (not opted in) | seam inert, no heavy imports |

---

## Implementation Units

### U1. Extract `ChunkManifest` seam

**Goal:** Move manifest production (mode selection + `blocked_intervals` gathering + partial-file cleanup) into a `ChunkManifest` class injected as the `PipelineStageRunner` `manifest=` step.

**Requirements:** R1, R4, R6

**Dependencies:** None

**Files:**
- Create: `src/screencap/chunk_manifest.py`
- Modify: `src/screencap/chunk_processor.py` (construct `ChunkManifest` in `__init__`; `_run_agnostic_stages` injects `self._chunk_manifest.produce`; remove the `_manifest_step` body + `_generate_manifest`; stop storing `self._segmentation_mode`)
- Test: `tests/test_chunk_manifest.py`
- Migrate: `tests/test_chunk_processor.py` — the tests that patch/replace `cloud_processor._generate_manifest` (multiple sites) must be re-pointed at the `ChunkManifest` seam (patch `chunk_manifest.produce` or the injected step), since `_generate_manifest` no longer exists on `ChunkProcessor`.

**Approach:**
- `ChunkManifest(capture_dir, *, segmentation_mode, rest_threshold, screen_filter=None)`.
- `produce(idx, start_ts, end_ts) -> Path`: gather `blocked_intervals` from `screen_filter.get_blocked_intervals` (guarded exactly as today — `hasattr` check, swallow+log on exception, `[] → None`), call `task_manifest.generate_manifest(..., segmentation_mode=self._mode)`, and on failure unlink the partial `chunk_{idx:04d}_manifest.json` before re-raising (preserve [chunk_processor.py:688-695](src/screencap/chunk_processor.py) behavior verbatim).
- In `_run_agnostic_stages`, the injected `manifest` step becomes: `self._set_status("Generating manifest..."); return self._chunk_manifest.produce(i, s, e)`. The `_set_status` call stays in the closure (status is a processor concern); everything else moves into the seam.
- `generate_manifest` itself is **unchanged**.

**Patterns to follow:** the existing injected-step closures in `_run_agnostic_stages`; `PipelineStageRunner`'s `ManifestStep` type signature `(idx, start, end) -> Path`.

**Test scenarios:**
- Happy path: `produce()` with `segmentation_mode="llm"` over a fixture `capture_dir` writes `chunk_0000_manifest.json` in v2 shape.
- Happy path: `produce()` with `segmentation_mode="idle"` writes the v1 idle-gap-segmented manifest.
- Edge case: `screen_filter=None` → manifest produced with `blocked_intervals=None`, no crash.
- Edge case: `screen_filter.get_blocked_intervals` returns `[]` → `None` is passed downstream (empty coalesced to `None`).
- Error path: `get_blocked_intervals` raises → exception logged, `blocked_intervals=None`, manifest still produced (no re-raise).
- Error path: underlying generate raises → partial `chunk_0000_manifest.json` is unlinked and the exception re-raises (assert no truncated file remains).
- Integration / leverage: construct `ChunkManifest` with only `capture_dir` + mode + `rest_threshold` (no `ChunkProcessor`, no recorder, no queues) and produce a manifest — proves R6.

**Verification:** `tests/test_chunk_manifest.py` passes; `tests/test_chunk_processor.py` manifest assertions and `tests/test_llm_segmentation.py` stay green; `ChunkProcessor` no longer references `self._segmentation_mode`.

---

### U2. Extract `ChunkScrubber` per-chunk execution seam

**Goal:** Move per-chunk `Scrubber` construction + `run_chunk` + audit logging + the "is scrubbing on?" predicate into a `ChunkScrubber`, replacing the inline `_scrub_chunk_files` call and its None-guard. Behavior-preserving; the scrub-config still lives in `ChunkProcessor.__init__` for now and is passed into `ChunkScrubber` (U3 moves the init itself).

**Requirements:** R2, R4, R6

**Dependencies:** None (can land in parallel with U1)

**Files:**
- Create: `src/screencap/chunk_scrubber.py`
- Modify: `src/screencap/chunk_processor.py` (construct `ChunkScrubber` from the existing config fields; `_process_chunk` step 5 calls `self._chunk_scrubber.scrub(...)`; remove `_scrub_chunk_files`)
- Test: `tests/test_chunk_scrubber.py`
- Migrate: `tests/test_chunk_processor.py` — the `test_scrub_chunk_files_*` test(s) that call `cloud_processor._scrub_chunk_files(...)` directly must move to `tests/test_chunk_scrubber.py` against the seam (`_scrub_chunk_files` no longer exists on `ChunkProcessor`).

**Approach:**
- `ChunkScrubber(capture_dir, *, enabled, pipeline, anonymizer, evaluator, classifier, pixel_ratio)` (config passed in for U2; constructed by U3). **Carry `pixel_ratio=2.0` (the Retina default) explicitly** — `ChunkProcessor` defaults it to `2.0` while the bare `Scrubber` may default differently, so the move must pass the value through, not rely on a downstream default, or masking pixel geometry shifts.
- `is_enabled -> bool`: encapsulates `enabled and pipeline is not None`.
- `scrub(idx, start_ts, end_ts, transcript_path) -> ScrubResult | None`: returns `None` when `not is_enabled` (no `Scrubber` constructed); otherwise constructs `Scrubber(...)` with the identical args as today ([chunk_processor.py:1103-1110](src/screencap/chunk_processor.py)), calls `run_chunk`, logs the audit-entry count, returns the `ScrubResult`.
- Sequencer: replace `if self._scrub_enabled and self._pipeline is not None:` with `scrub_result = self._chunk_scrubber.scrub(...)` then `if scrub_result is not None:` to gate the content-index pass (the index pass must still branch on a real result, never on the scrub being attempted). **Note:** the content-index pass also has its own internal masking-context fail-closed guard that reads the masking fields; re-sourcing that guard from the seam is handled in U3 (R7), since U3 is where those fields move.

**Execution note:** behavior-preserving — assert pass-through of every `Scrubber` constructor arg with a fake/spy `Scrubber` so the move is provably faithful before U3 changes where the config comes from.

**Patterns to follow:** `_scrub_chunk_files`'s existing structure and docstring (it already frames the processor as owning only lifecycle concerns).

**Test scenarios:**
- Happy path: enabled `ChunkScrubber.scrub()` constructs a `Scrubber`, returns its `ScrubResult`; audit-entry count logged when `audit_entries` non-empty.
- Edge case: `is_enabled=False` → `scrub()` returns `None` and constructs no `Scrubber` (assert via spy).
- Edge case (pass-through): every arg (`pipeline`, `anonymizer`, `evaluator`, `classifier`, `pixel_ratio`) reaches the `Scrubber` constructor unchanged.
- Integration: the returned `ScrubResult.blocked_intervals` is the exact object the sequencer feeds to `_index_chunk_content` (seam returns it unmodified).
- Leverage: construct `ChunkScrubber` and call `scrub()` with no `ChunkProcessor` present — proves R6.

**Verification:** `tests/test_chunk_scrubber.py` passes; `tests/test_chunk_processor.py` scrub + content-index tests stay green; `ChunkProcessor` no longer defines `_scrub_chunk_files`.

---

### U3. Move scrub/masking-config init into `ChunkScrubber` and de-couple the upload-disable side effect

**Goal:** Relocate the `_should_init_scrub` block from `ChunkProcessor.__init__` into `ChunkScrubber`, replace the cross-boundary `_upload_enabled` / `_upload_disabled_reason` mutation with an explicit init result the sequencer consumes (R3), and re-source the content-index fail-closed masking guard from the seam so it survives the field removal (R7).

**Requirements:** R3, R5, R7

**Dependencies:** U2

**Files:**
- Modify: `src/screencap/chunk_scrubber.py` (factory builds pipeline + anonymizer + masking classifier/evaluator + pixel_ratio + cloud-intent PUBLIC override; returns a `ScrubInit` result; exposes `has_masking_context: bool`)
- Modify: `src/screencap/chunk_processor.py` (`__init__` calls the `ChunkScrubber` factory; consumes the result to set `_upload_enabled=False` + `_upload_disabled_reason` + re-assert `_auto_delete=False` when the result says so; deletes the inlined init block and the `_pipeline`/`_anonymizer`/`_masking_*` fields; `_do_index_chunk_content`'s guard reads `self._chunk_scrubber.has_masking_context` instead of the deleted fields)
- Test: `tests/test_chunk_scrubber.py` (extend)
- Migrate: `tests/test_chunk_processor.py` — the `cloud_processor` fixture (used by ~21 tests) currently sets `cp._pipeline` / `cp._anonymizer` directly and asserts `cp._pipeline is None`; these must construct/inject a `ChunkScrubber` (real or fake) instead of poking deleted fields. The init-failure assertions that check `_upload_disabled_reason` stay but assert the value flowed from `ScrubInit`.

**Approach:**
- Factory signature roughly `ChunkScrubber.create(capture_dir, *, cloud_intent, upload_enabled, scrub_enabled, privacy_mode) -> (ChunkScrubber, ScrubInit)` where `ScrubInit{disable_uploads_reason: str | None}`. The `ChunkScrubber` carries `is_enabled` + `has_masking_context` + the masking config; `ScrubInit` carries only the upload-policy fact.
- **`has_masking_context`** is `True` iff both the masking classifier and evaluator were built (the exact predicate the content-index guard needs). `_do_index_chunk_content` reads it through the seam; the guard's fail-closed semantics ([chunk_processor.py:1161-1175](src/screencap/chunk_processor.py)) are otherwise byte-for-byte. The guard only needs the boolean — it does not use the classifier/evaluator objects (frame-skipping uses `scrub_result.blocked_intervals`), so no object re-exposure is required.
- **`ScrubInit` consumption order is a hard done-criterion:** the sequencer must set BOTH `self._upload_enabled = False` AND `self._upload_disabled_reason = scrubit.disable_uploads_reason` together when `disable_uploads_reason` is non-None, *before* any chunk is processed. This is load-bearing: `_process_chunk` computes `success = self._upload_disabled_reason is None` ([chunk_processor.py:808](src/screencap/chunk_processor.py)). Setting `_upload_enabled=False` but forgetting `_upload_disabled_reason` flips `success` to `True` → chunk marked `EMITTED` → `stub_recording()` deletes local files with nothing on GCS — the exact Bug 4 data-loss scenario. Then the existing end-of-`__init__` invariant (`if not _upload_enabled and _auto_delete: _auto_delete=False`) fires unchanged.
- **`create()` must be fail-closed under cloud-intent:** if the factory itself raises, a cloud-intent recording must NOT end up constructed with `_upload_enabled=True` and no scrubbing. Wrap the factory call so any uncaught failure under `cloud_intent and upload_enabled` disables uploads with a reason (same posture as today's inline try/except).
- Preserve the decision matrix verbatim (see High-Level Technical Design): cloud-intent init failure → `disable_uploads_reason` set; local opt-in init failure → `is_enabled=False`, no upload disable; classifier-init failure under cloud-intent → also disables uploads.
- **No new coupling booleans** (SCR-30→SCR-110 lesson): move the config construction as-is; do not introduce a flag that gates one scrub operation on another. (`has_masking_context` is not a coupling boolean — it exposes existing state for the same guard that already read it.)

**Test scenarios:**
- Happy path (local opt-in, deps present): `is_enabled=True`, `has_masking_context=True`, `disable_uploads_reason=None`.
- Happy path (cloud-intent): masking evaluator built with `PrivacyMode.PUBLIC` (assert mode).
- Error path (cloud-intent, deps missing): `ScrubInit.disable_uploads_reason` set; sequencer disables uploads and forces `_auto_delete=False`; assert `ChunkScrubber` performed **no** direct mutation of processor state.
- Error path (local opt-in, deps missing): `is_enabled=False`, uploads NOT disabled, recording proceeds.
- Error path (cloud-intent, classifier init fails): uploads disabled with a reason AND `has_masking_context=False`.
- Error path (factory raises under cloud-intent): uploads disabled with a reason (fail-closed), not left enabled.
- **Bug-4 guard (integration, in `tests/test_chunk_processor.py`):** when `ScrubInit.disable_uploads_reason` is non-None, `_process_chunk`'s settle computes `success=False` → chunk `FAILED` (not `EMITTED`), so the sentinel/stub gate blocks and nothing is deleted. `Covers` the never-delete-without-confirm invariant.
- **Content-index guard (R7):** with `has_masking_context=False`, `_do_index_chunk_content` indexes nothing (assert no rows written); with `True`, it proceeds as today.
- Edge case (not opted in): factory returns an inert `ChunkScrubber` (`is_enabled=False`, `has_masking_context=False`) and imports no redaction/privacy modules.

**Verification:** the data-loss tests in `tests/test_chunk_processor.py` covering disabled-upload / privacy-init-failure paths (the Bug 4 / `_upload_disabled_reason` cases) stay green (after the fixture migration above); `ChunkProcessor.__init__` no longer contains the `_should_init_scrub` block or the `_pipeline`/`_anonymizer`/`_masking_*` fields; `_do_index_chunk_content` no longer references a `ChunkProcessor` masking field.

---

### U4. Finalize `ChunkProcessor` as a sequencer (verification / cleanup checkpoint)

**Goal:** A small cleanup/narrative checkpoint after U1–U3 — not an independent extraction. Update the class and `_process_chunk` docstrings to name the rotate → manifest → scrub → upload → delete sequence, and assert structurally that the extracted concerns are gone. (Most dead-code removal already lands inside U1–U3; this unit confirms the end state and documents the sequence. If U1–U3 leave nothing to remove, U4 collapses to the docstring + the structural assertion.)

**Requirements:** R4

**Dependencies:** U1, U2, U3

**Files:**
- Modify: `src/screencap/chunk_processor.py` (docstring + any residual dead-code sweep; confirm only `self._chunk_manifest` / `self._chunk_scrubber` remain of the extracted concerns)

**Approach:**
- No behavioral change. Audit that `_segmentation_mode`, `_pipeline`, `_anonymizer`, `_masking_classifier`, `_masking_evaluator`, `_masking_pixel_ratio`, `_generate_manifest`, `_manifest_step` body, and `_scrub_chunk_files` are all gone, and that `_do_index_chunk_content` reads `self._chunk_scrubber.has_masking_context` rather than a deleted field.

**Test scenarios:**
- Add a structural assertion test that the removed attributes/methods no longer exist on `ChunkProcessor` (`not hasattr(cp, "_pipeline")`, etc.) — cheap and catches a regression that re-introduces a field. Beyond that, no behavioral test (no behavioral change).

**Verification:** the migrated `tests/test_chunk_processor.py` suite is green; the `_process_chunk` body reads as the 7-step sequence with manifest/scrub delegated to the seams.

---

### U5. End-to-end re-validation across the unified path

**Goal:** Prove the refactor preserved every load-bearing invariant — data-loss rules, privacy masking, the terminal-stage heartbeat, and content-index gating — across the real pipeline, not just the unit seams.

**Requirements:** R5

**Dependencies:** U1, U2, U3, U4

**Files:**
- Modify (if gaps found): `tests/test_chunk_processor.py`, `tests/test_recording_integration.py`
- Verify (no edit expected): `tests/test_terminal_stage.py`, `tests/test_scrubber_class.py`, `tests/test_pipeline_stages.py`, `@pytest.mark.privacy` masking guards

**Approach:**
- Run the `tests/test_chunk_processor.py` **data-loss invariant** tests (closed-set seeding, tri-state status, `was_force_stopped` poisoning, never-delete-without-confirm, `_auto_delete=False` when uploads off) — these assert *runtime behavior* and must stay green. (Note: the *scrub/manifest-internals* tests in the same file are migrated to the seam test files in U1–U3; "behavior-preserving" applies to runtime behavior, not to tests that poked now-deleted private fields.)
- Confirm the SCR-133 privacy-guard CI lanes pass after the refactor: `privacy-guards-macos` (Vision) and `privacy-guards-visionfree` (fake OCR stack forcing the masking path). These run on every PR, so green CI here is real signal that background-window masking still routes through `ChunkScrubber`. A local Vision-Mac run of the `@pytest.mark.privacy` guards is a supplementary check, not the sole gate.
- Confirm `tests/test_terminal_stage.py` stays green — the terminal-stage `produce()` / `on_progress` heartbeat is untouched (no scrub relocation into the terminal stage).
- Confirm the SCR-118 content-index pass still (a) runs only when scrubbing produced a real `ScrubResult` and (b) refuses to index when `has_masking_context` is False (R7).

**Execution note:** characterization-first where a behavior is currently only proven by an integration run — add a focused unit test rather than relying on the integration suite alone.

**Test scenarios:**
- Integration: a recording with `cloud_intent=True` and deps present produces manifest + scrubbed/masked outputs and uploads exactly as before (end-to-end in `tests/test_recording_integration.py`).
- Integration: a force-stopped recording leaves chunks `PENDING` (not falsely `EMITTED`), and nothing is deleted without remote confirmation — runtime behavior unchanged.
- Privacy: both SCR-133 CI lanes (`privacy-guards-macos`, `privacy-guards-visionfree`) pass; local Vision-Mac `@pytest.mark.privacy` run passes.
- Content-index R7: a chunk processed with `has_masking_context=False` writes zero index rows; with `True`, indexing proceeds as before.
- Heartbeat: `tests/test_terminal_stage.py` `on_progress` / `upload_preparing` assertions stay green.

**Verification:** full suite green (`PYTHONPATH=src pytest tests/`), both privacy CI lanes green, no diff to upload/delete/eviction/terminal-stage code.

---

## System-Wide Impact

- **Interaction graph:** `ChunkProcessor` → `PipelineStageRunner` (manifest step now `ChunkManifest.produce`) and `ChunkProcessor` → `ChunkScrubber` → `Scrubber`. New cross-dependency: `_do_index_chunk_content` (stays on `ChunkProcessor`) → `ChunkScrubber.has_masking_context` for its fail-closed guard. No change to `ChunkProcessor` → `terminal_stage` / `retention` / `PipelineLedger`.
- **Error propagation:** manifest failure still re-raises out of the injected step so `PipelineStageRunner` does not reach `mark_staged` (chunk stays pre-STAGED, re-runnable) — `ChunkManifest.produce` must re-raise after cleanup, never swallow. Scrub-init failure now propagates as an explicit `ScrubInit` value rather than a side effect.
- **State lifecycle risks:** the scrub-init → upload-disable → `_auto_delete=False` chain is the highest-risk move (R3). The sequencer must apply the disable + re-assert the safety invariant; a missed application would re-open Bug 4 (delete-with-nothing-on-GCS). Covered by U3 tests + U5 regression.
- **API surface parity:** none — both seams are internal; `ChunkProcessor`'s public methods (`start`/`stop`/`status`/`reconcile_against_gcs`/...) are unchanged.
- **Integration coverage:** the `@pytest.mark.privacy` masking guards and `tests/test_recording_integration.py` are the cross-layer proofs unit tests alone won't give.
- **Unchanged invariants:** the five data-loss-prevention rules, the terminal flock + `on_progress` heartbeat, the cloud window filter in `_export_events` (and its `test_privacy_filter_call_graph.py` guard), and the `recording.db`-is-local-only rule. This plan explicitly does not touch any of them.

---

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| **Content-index fail-closed guard reads `_masking_classifier`/`_evaluator` that U3 deletes → guard silently always-skips, or (if mis-wired) masked-app text leaks into the local index** | R7 + U3: `ChunkScrubber.has_masking_context` re-sources the guard from the seam; `_do_index_chunk_content` reads the seam predicate; U3/U5 test that `has_masking_context=False` indexes nothing |
| Moving the scrub-init upload-disable side effect drops a case → silent data loss (Bug 4 class) | U3 reproduces the full decision matrix in tests; **`_upload_enabled` + `_upload_disabled_reason` set together** (consumption-order done-criterion); sequencer re-asserts `_auto_delete=False`; U5 runs the data-loss invariant suite as oracle |
| `tests/test_chunk_processor.py` is coupled to the deleted symbols (`_pipeline`/`_anonymizer`/`_generate_manifest`/`_scrub_chunk_files`, ~52 references) so it cannot "stay green unchanged" | U1–U3 each migrate their coupled tests onto the seam test files; R5/U5 scope "unchanged" to runtime behavior (data-loss invariants), not to private-field tests |
| Privacy masking silently breaks but is missed by CI | **Corrected:** SCR-133 added two PR-gating lanes (`privacy-guards-macos`, `privacy-guards-visionfree`) — U5 verifies both pass; a local Vision-Mac run supplements |
| Moving redaction/privacy imports into `chunk_scrubber.py` trips an import-lightness or package-boundary guard | Verify `tests/test_package_boundary_call_graph.py`, `tests/redaction/test_import_lightness.py`, `tests/test_privacy_filter_call_graph.py` pass; keep imports deferred inside `ChunkScrubber` exactly as today |
| Content-index pass accidentally re-gated on "scrub attempted" vs "scrub produced a result" | U2 gates the index pass on `scrub_result is not None`; test asserts the off-path skips indexing |
| `pixel_ratio` Retina default (`2.0`) lost in the move → masking pixel geometry shifts | U2 passes `pixel_ratio=2.0` through to `Scrubber` explicitly; pass-through scenario asserts it |

---

## Alternative Approaches Considered

- **Approach B — absorb the v1/v2 dispatch into `ChunkManifest`, reduce `generate_manifest` to a shim.** Rejected as the default: it either duplicates the dispatch branch or forces the non-pipeline callers (config, tests) through a chunk-oriented class, for no locality gain on those callers. Approach A already gives the chunk pipeline single-owner mode selection. Revisit only if a second mode-consuming pipeline path appears.
- **Extract a third `ChunkUploader` / eviction seam too.** Out of scope: post-SCR-125 that logic already delegates to `terminal_stage` / `retention` / `PipelineLedger`, it is the most data-loss-prone code, and the issue's Solution names only two seams. Carving it now would add risk without the issue asking for it.
- **One combined `ChunkScrubber` unit (init + per-chunk in a single commit).** Rejected in favor of U2 (behavior-preserving wrapper) then U3 (move init + de-couple side effect) so the risky upload-disable change lands in isolation with the per-chunk move already proven faithful.

---

## Documentation / Operational Notes

- No user-facing or operational change (internal refactor; no flags, no schema, no API).
- After landing, capture the god-object-decomposition / seam-extraction convention via `/ce-compound` — no such learning doc exists and this is a clean exemplar (flagged by learnings research).

---

## Open Questions

### Resolved During Planning

- *Does `ChunkManifest` need to own the v1/v2 render branch?* — No. It owns mode selection; `generate_manifest` stays the renderer (Approach A).
- *Should upload/delete become a third seam?* — No; out of scope (issue names two seams; logic already delegates post-SCR-125).
- *Is the prerequisite `Scrubber` available?* — Yes, `src/screencap/scrubber.py:2102`.

### Deferred to Implementation

- `ScrubInit` shape and build style (`__init__` vs `create()` classmethod) — settle when wiring U3 against the real `__init__` ordering. **Prefer a named dataclass over a plain tuple** so `disable_uploads_reason` can't be positionally misassigned at the call site (a silent bug that passes stubbed tests).
- Predicate authority for "is scrubbing on?" during the U2→U3 window: U2 introduces `ChunkScrubber.is_enabled` while U3 still leaves the deps-missing `_scrub_enabled=False` downgrade in `ChunkProcessor.__init__`. Land U2 so the sequencer reads `self._chunk_scrubber.is_enabled` (single authority) rather than a copy of `self._scrub_enabled` taken before the downgrade — otherwise a deps-missing local recording could call `scrub()` against a `None` pipeline in the interim. (Cleanest: build the `ChunkScrubber` from the already-downgraded flag in U2.)
- Whether the `_set_status("Generating manifest...")` call stays in the injected closure or moves behind a tiny status callback passed to `ChunkManifest` — decide during U1; default is to keep status in the processor closure.

---

## Sources & References

- **Linear issue:** [SCR-35 — Pull ChunkManifest and ChunkScrubber out of ChunkProcessor](https://linear.app/zk-email/issue/SCR-35/pull-chunkmanifest-and-chunkscrubber-out-of-chunkprocessor)
- Prior architecture plan: [docs/plans/2026-06-09-001-refactor-scr-125-unified-per-chunk-upload-plan.md](docs/plans/2026-06-09-001-refactor-scr-125-unified-per-chunk-upload-plan.md)
- Code: `src/screencap/chunk_processor.py`, `src/screencap/pipeline_stages.py`, `src/screencap/scrubber.py`, `src/screencap/task_manifest.py`
- Learnings: `docs/solutions/runtime-errors/chunk-upload-sentinel-gating-and-data-loss.md`, `docs/solutions/integration-issues/inner-timeout-unreachable-behind-outer-watchdog-2026-06-24.md`, `docs/solutions/workflow-issues/privacy-guards-deselected-on-ci-silently-rot-2026-06-10.md`
