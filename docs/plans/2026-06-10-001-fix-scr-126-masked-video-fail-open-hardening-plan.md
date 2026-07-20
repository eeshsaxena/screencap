---
title: "fix: SCR-126 masked-video-upload fail-open hardening"
type: fix
status: completed
date: 2026-06-10
deepened: 2026-06-10
---

# fix: SCR-126 masked-video-upload fail-open hardening

## Summary

Close three latent fail-open holes in the dormant `masked_video_upload` ON path so the flag can be safely flipped later: (1) add an explicit upload-seam assertion that a cloud `chunk_*.mp4` is the masker's output and never the rich source; (2) make the post-hoc masking coverage gate prove coverage over each chunk's *actual decoded PTS extent* (not a uniform `chunk_dur` span) plus a fail-closed in-masker guard, so trailing frames can never ship clear; (3) record masked-video provenance so the scrubbed-copy reuse path can never re-upload a stale or under-masked `masked_video/` copy. The flag default **stays OFF** — this is hardening of the dormant path, not enablement.

---

## Problem Frame

The unified pipeline already routes cloud chunk video through a post-hoc masker when `masked_video_upload` is ON, gated by a fail-closed three-way coverage gate (`video_mask.mask_video_chunk`). The flag ships OFF, so capture-time blocking is the live structural guarantee and these paths are dormant. A code review of the SCR-125/SCR-129 work surfaced three places where the dormant ON path **fails open** — i.e., could let unmasked or under-masked rich video reach GCS. Each must be closed *before* the flag is ever enabled. The work is high-risk by nature: this is the single highest privacy-risk component in the pipeline, and the dominant failure mode (per `docs/solutions/integration-issues/mitmproxy-ignore-hosts-tunneling-all-flows-2026-04-29.md`) is a "no-op that looks like success."

---

## Requirements

- R1. **Upload-seam masked assertion at EVERY enumeration path (Fix 1).** When the frozen `masked_video_upload` decision is ON for a recording, *no* `chunk_*.mp4` whose local path is not the masker's output (`<name>-scrubbed/masked_video/`) may be enqueued for cloud upload — the seam hard-rejects (fail closed). The gate is applied at every site that enumerates/enqueues chunk media: the `upload_chunk_files` `FileInfo` construction, the reconcile-path `FileInfo` construction, **and** the scrubbed-dir `list_recording_files`/`upload_recording` rglob path (which today bypasses any masked-video check). "Shared chokepoint" is insufficient — the rglob path does not pass through `upload_chunk_files`.
- R2. **Coverage over real frame extent (Fix 2).** No frame may be encoded into a cloud copy outside a coverage-proven span. A chunk whose real decoded frames extend beyond the caller's nominal span must mask those frames or fail closed — it must never report `MASKED`/`UNMASKED_PROVABLY_SAFE` while shipping trailing clear frames.
- R3. **Real per-chunk span derivation, reconciled with recovery (Fix 2).** The masker proves coverage over the chunk's own decoded PTS extent. The per-chunk absolute origin and last-chunk extension are derived through a **shared helper** consumed by both `terminal_stage` and `recovery._recover_chunk_metadata` (which extends the last chunk to `last_ts + 1.0`), so the masked video and the recovery-generated manifests/events cannot reason over different chunk boundaries. Legitimate last/VFR chunks pass the gate rather than spuriously failing closed.
- R4. **Reuse provenance + closed-set reconciliation (Fix 3).** The scrubbed-copy reuse path can never re-upload a stale/under-masked `masked_video/` copy. Masked-video provenance (per-source-chunk byte fingerprint + independent geometry hash + privacy-policy fingerprint + `pixel_ratio` + frozen flag + `MASK_PROVENANCE_VERSION`) gates reuse; on every `produce`, `masked_video/` is reconciled as a CLOSED SET against the current expected chunk set — any orphaned, FAILED, or provenance-mismatched `masked_video/chunk_*.mp4` is purged before upload, including on the reuse path (which skips the wholesale rebuild) and for chunk indices the current pass never visits.
- R5. **Docs in sync (Doc).** `SECURITY.md`'s masked-video section stays accurate after the hardening (explicit upload-seam assertion at all paths, real-extent + wall-clock-aligned coverage, reuse provenance, and the convergence-fast-path limitation). The flag default remains OFF and the enablement prerequisites remain documented.
- R6. **Fail-closed on uncertainty (cross-cutting).** Every gate fails CLOSED on uncertainty. The tri-state distinction "masking disabled" ≠ "masking failed" ≠ "masking confirmed" is preserved; only an authoritative masked / provably-safe signal unlocks the cloud upload. A best-guess or absent signal never authorizes an upload.
- R7. **Per-frame lookup aligned to true wall-clock; origin precision is a SAFETY property (Fix 2).** The masker maps each frame to a geometry interval via `abs_ts = chunk_start_abs + frame_pts·time_base`. An absolute origin (`chunk_start_abs`) *earlier* than the chunk's true first-frame wall-clock shifts every lookup earlier and can mask a window from *before* a sensitive window opened → ship the sensitive frame clear (a leak the coverage gate does not catch, since it proves geometry density in DB-timestamp space, not lookup alignment). Therefore the origin must be derived from an authoritative source, and a chunk whose origin alignment cannot be proven (e.g., geometry samples do not bracket the decoded frames) must FAIL CLOSED — not be treated as "accuracy-only." (Verified: the capture encoder computes PTS from absolute elapsed time per frame — `engine/video.py:160` — so idle gaps do *not* accumulate skew; the monotonic clamp at `:163` only nudges sub-frame-interval bursts forward by one tick, which over-masks (safe). The residual real risk is the absolute origin, addressed here.)
- R8. **Convergence fast path must not serve stale-logic copies (Fix 3).** The already-converged fast path (`finalize_gate_satisfied` early-return) skips `produce()`/masking/provenance entirely. It must not declare convergence for a recording whose cloud video was produced under an older `MASK_PROVENANCE_VERSION`; the fast path is made provenance-version-aware (re-mask / re-converge on a version bump), or the limitation is explicitly documented and accepted in `SECURITY.md`. This plan chooses provenance-awareness (privacy gate).
- R9. **Live/terminal parity under the new masker authority (Fix 2).** U2 makes the masker authoritative for the span on BOTH paths; the live caller's exact `end_ts`/`rotation_time` becomes advisory. The live path is therefore NOT unaffected — it is subject to the same fail-closed guard. The guard must be proven not to spuriously `FAILED` honest action-gated VFR live input (where the last decoded frame's PTS precedes `rotation_time`).

---

## Scope Boundaries

- **Do NOT flip the flag.** `config.get_masked_video_upload_enabled()` stays default **False** (`src/screencap/config.py:381`). This plan hardens the dormant ON path only.
- **No change to capture-time blocking.** The structural OFF-path guarantee (`RecorderPrivacyFilter` dropping sensitive windows at capture) is untouched.
- **No re-implementation of the masker's spatial masking** (hold-and-pad, dilation, `_windows_to_rects`) or the privacy classifier — only the *temporal span* the gate reasons over.
- SCR-125 live-finalize cutover (the blocking ticket) is already merged; not re-done here.

### Deferred to Follow-Up Work

- **Swift native-redaction-review consent-gate** (SCR-126 prerequisite #2 — `macos/Screencap/Views/Review/ReviewWindow.swift:317` "Local preview — not uploaded"): deferred to the future flag-flip ticket. While the flag is OFF the label is accurate; changing it now would mislead operators. Related origin context: `docs/brainstorms/2026-06-03-native-redaction-review-before-upload-requirements.md`.
- **`/ce-compound` capture** of three documented-learning gaps after this lands: masked-video coverage-gate design, recovery↔terminal chunk-range reconciliation, and scrubbed-copy reuse/provenance correctness (none currently exist in `docs/solutions/`).

---

## Context & Research

### Relevant Code and Patterns

- **Fix 1 seam** — `src/screencap/upload.py:194` `assert_uploadable` + `_is_raw_artifact` denylist (`upload.py:177-209`): the existing R8 hard-gate pattern to mirror for a sibling `assert_video_masked`. Both live and terminal enqueues converge on `chunk_processor.upload_chunk_files` (`src/screencap/chunk_processor.py:1268`) and the reconcile path (`chunk_processor.py:362-394`), each constructing `FileInfo` and calling `assert_uploadable` — that construction site is the shared chokepoint. Live masked-path switch: `chunk_processor._collect_chunk_files` (`chunk_processor.py:1097-1104`); terminal: `terminal_stage._upload_source_chunk_media` (`src/screencap/terminal_stage.py:1003-1012`, whose comment already flags this as "folded into SCR-126 when the flag flips").
- **Fix 2 masker** — `src/screencap/video_mask.py`: `_build_coverage` (`157-208`, three-way gate, `MAX_GEOMETRY_GAP_SECONDS = 2.0`), `_build_sensitive_intervals` (`256-287`, last interval held to `end_ts`), `_interval_for` (`317-339`, returns `None` past the last interval), `mask_video_chunk` (`341-498`), `_transcode_with_masks` (`501-617`, maps `abs_ts = chunk_start_abs + frame_pts*time_base` at `564` and draws clear when `iv is None`). Caller span math: `terminal_stage._chunk_timing` (`terminal_stage.py:456-483`) + `_mask_videos` (`408-453`). Reconcile target: `recovery._recover_chunk_metadata` chunk-range loop (`src/screencap/recovery.py:128-134`). Live path supplies exact spans (`chunk_processor.py:704-705`).
- **Fix 3 reuse** — `src/screencap/scrubber.py`: `is_scrubbed_copy_reusable` (`3076-3101`), `_compute_source_hash` (`3011-3036`), `_write_scrub_sentinel` (`3039-3073`), `_SOURCE_HASH_GLOBS` (`79-87` — includes `recording.db`/`-wal` and `.recording_intent`, **excludes** source `chunk_*.mp4` media and global privacy-policy config), `mask_video_chunk_for_cloud` (`2297-2366`, re-masks every call, but on coverage-FAILED returns without removing a pre-existing `output_path`), `masked_video_dir` (`2284-2294`). Orchestration: `terminal_stage.CloudCopyProducer.produce` (`terminal_stage.py:353-406`) runs recovery → scrub(sentinel) → `_mask_videos` (masking is step 3, *after* the scrub sentinel).
- **Frozen-flag resolution** — `pipeline_chunk_ops.get_frozen_masked_video_upload` (`src/screencap/pipeline_chunk_ops.py:49-67`): the single source for a recording's frozen decision; all gates must use this, never the mutable global.

### Institutional Learnings

- `docs/solutions/integration-issues/mitmproxy-ignore-hosts-tunneling-all-flows-2026-04-29.md` — a safety gate that fails to a no-op is indistinguishable from success; the load-bearing prevention is an **adversarial end-to-end EFFECT test** asserting the artifact actually changed. → Fix 2 needs a test that decodes the cloud copy and asserts the trailing/out-of-nominal-span frames are masked (or the chunk FAILED), not just "the masker ran."
- `docs/solutions/runtime-errors/chunk-upload-sentinel-gating-and-data-loss.md` — distinguish disabled/failed/succeeded with a tri-state; only an explicit success token may permit the irreversible action; confirm the precondition NOW; track the full closed set. → Fix 1's assertion is that authoritative token; Fix 3 must not let a stale copy read as "confirmed."
- `docs/solutions/runtime-errors/capture-health-nonscreen-attribution-terminal-stop-2026-06-01.md` — a heuristic/best-guess signal must never drive an irreversible action; test the *invariant*, not the labeller. → bias every ambiguous span/coverage decision to fail closed; assert "uncertain coverage never uploads clear."
- `docs/solutions/bug-fixes/video-pts-offset-bframe-corruption-20260322.md` — action-gated VFR video has non-trivial PTS: `video_start_time` is stamped at first frame, `bf=0` so `pts==dts`, never override `packet.pts` after `encode()`; the old tests passed because they only checked `last_pts > 0` on a corrupt file. → Fix 2 must derive spans from real decoded packet PTS and test that PTS starts/extends where expected; **re-confirm where the chunk's absolute first-frame time comes from in the current SCR-125 disk-first pipeline** (this doc predates it).
- `docs/solutions/integration-issues/review-data-nullable-timing-swift-consumer-2026-06-01.md` — an action-empty chunk is a valid, playable, zero/null-timing state. → Fix 2's extent derivation must treat a zero/near-zero-span chunk as legitimate (fail closed if coverage is genuinely unprovable, but don't crash or mis-handle it).

### External References

- None required — this is internal hardening of an existing, well-understood component; local patterns (`assert_uploadable`, the three-way gate, the scrub sentinel) are the references to follow.

---

## Key Technical Decisions

- **Fix 1 is a gate applied at EVERY `assert_uploadable` call site plus the scrubbed-dir enumeration seam — not a single chokepoint.** A sibling of `assert_uploadable` (`assert_video_masked`) inspects the `FileInfo`: when the resolved frozen flag is ON and the name matches `chunk_*.mp4`, the local `path` MUST be under a `masked_video/` directory, else raise. Path-structural (not content-inspecting) keeps it cheap and deterministic; pairing it with U3 provenance is the right division of labor (path proves "came from the masker dir"; provenance proves "current/correct"). Deepening review corrected the placement: live + terminal chunk-media enqueues converge on `upload_chunk_files` (`chunk_processor.py:1290`), but the reconcile path constructs its own `FileInfo` (`chunk_processor.py:382`) and the terminal stage *also* ships the scrubbed dir via `upload_recording(scrubbed_dir)` → `list_recording_files` → `assert_uploadable` (`terminal_stage.py:848`, `upload.py:381`), which never touches `upload_chunk_files`. The gate must therefore be a function applied at each `assert_uploadable` site AND wired into the `list_recording_files`/`upload_recording` enumeration (resolving the frozen flag from the recording dir), or the plan must structurally prove no `chunk_*.mp4` can be enumerated by `list_recording_files(scrubbed_dir)` outside `masked_video/`. The frozen flag is resolved in the caller via `get_frozen_masked_video_upload` and passed into the gate (keeps `upload.py` free of a `catalog`/`.recording_intent` dependency; honors frozen-not-global).
- **Fix 2 makes the masker authoritative for span on BOTH paths, and the per-frame lookup must be wall-clock-accurate.** Primary fix: `mask_video_chunk` derives `end_ts` from the chunk's own decoded PTS extent (a cheap demux probe of max packet PTS, before the coverage gate), so coverage is proven over the real frame extent and legitimate VFR/last chunks pass. Belt-and-suspenders: `_transcode_with_masks` fails CLOSED (raise → FAILED, drop the copy) on any decoded frame whose `abs_ts` is outside the proven span (beyond a float epsilon). **Safety correction from deepening review (R7):** the absolute origin `chunk_start_abs` is not "accuracy-only" — an origin earlier than the chunk's true first frame shifts every geometry lookup earlier and can ship a sensitive frame clear, which the coverage gate (density-in-DB-time) does not catch. So the origin must come from an authoritative source and a chunk whose origin/geometry alignment cannot be proven fails closed. **Parity correction (R9):** because the masker becomes authoritative, the live caller (exact `rotation_time`, which overshoots the last decoded frame) is *also* subjected to the guard; "live path unaffected" was wrong. The guard must be proven not to spuriously FAIL honest VFR live chunks. **Reconciliation (R3):** the origin + last-chunk-extension derivation is promoted into a shared helper used by both the terminal masker path and `recovery._recover_chunk_metadata` (which already owns the `last_ts + 1.0` arithmetic) — touching only the terminal copy while recovery keeps its own rule is what *creates* drift, so the shared origin/extension helper is in-scope for U2 (the fuller per-chunk `derive_chunk_ranges` remains deferred). Rejected alternatives: keeping the caller's uniform `chunk_dur` + only the guard (fail-closes every real last chunk); deferring all reconciliation (contradicts R3 and diverges manifests/events from the masked video).
- **Fix 3 adds an independently-validated masked-video provenance record AND a closed-set reconciliation of `masked_video/`.** Because masking is step 3 (after the scrub sentinel), reuse correctness for `masked_video/` cannot ride on `.scrub_complete`. A dot-prefixed provenance record (`masked_video/.masked_provenance.json`, excluded from upload by the dotfile filter) written atomically after a fully-successful pass captures, explicitly: a per-source-chunk byte fingerprint (the input `_SOURCE_HASH_GLOBS` omits), an independent `recording.db`/geometry hash (not delegated to the scrub sentinel), the privacy-policy fingerprint, `pixel_ratio` (changes every mask rectangle's geometry), the frozen flag, and `MASK_PROVENANCE_VERSION`. **Closed-set reconciliation (deepening H2):** the wholesale `masked_video/` rebuild only happens inside `scrub_recording` (rmtree+copytree), which the reuse path SKIPS — and a case-(b) coverage-FAILED leaves a pre-existing `output_path` intact, and an orphaned index (source chunk gone) is never visited by `_mask_videos`. So `produce` must reconcile `masked_video/` against the current expected chunk set on every run and purge any chunk not produced/validated by the current pass — not merely remove-on-FAILED — closing the stale-rglob-upload hole. **Convergence fast path (deepening H4 / R8):** the `finalize_gate_satisfied` early-return skips `produce`, so it must independently verify the masked-video provenance version before declaring convergence, else a recording converged under old mask logic is never re-masked. **TOCTOU:** validate-and-upload must occur under one continuous terminal-flock hold (it does today); the plan asserts no mutation of `masked_video/` between provenance-validate and upload.
- **`MASK_PROVENANCE_VERSION` is a U2 deliverable that U3 consumes.** The span fix (U2) changes masking behavior, so U2 must bump the version; U3's reuse/convergence checks key on it. Sequencing U2 → U3 makes this hold; a U2-only partial landing without the bump would leave a reuse hole.
- **All gates resolve the FROZEN per-recording decision, never the global — and an unresolvable frozen decision fails closed.** A mid-flight global flip must not change a recording's behavior; matches the existing SCR-125 frozen-decision rule. Deepening correction: `get_frozen_masked_video_upload` (`pipeline_chunk_ops.py:49-67`) falls back to the *mutable global* when `.recording_intent` is missing/corrupt/legacy. That fallback is acceptable for masking *decisions* (a legacy recording has no frozen value), but for the upload GATE it contradicts R6 ("an absent signal never authorizes an upload") once the global can be ON: a recording whose frozen decision is unreadable would be governed by the global rather than its capture-time posture. So the **upload-seam gate** (U1) treats an unresolvable frozen decision as fail-closed (reject a non-`masked_video/` chunk), independent of the global. No existing unit re-examined this fallback for the flag-ON world; U1 owns it.

---

## Open Questions

### Resolved During Planning

- *Where does Fix 1's assertion belong?* → A reusable `assert_video_masked` gate in `upload.py`, applied at **every** `assert_uploadable` call site (`upload_chunk_files`, the reconcile-path construction) AND wired into the `list_recording_files`/`upload_recording` enumeration — not a single chokepoint. The deepening review verified the scrubbed-dir rglob (`terminal_stage.py:848`) bypasses `upload_chunk_files`.
- *Fix 2: caller-derived spans vs. masker-derived?* → Masker-derived `end_ts` from the decoded PTS extent (primary) + in-loop fail-closed guard (secondary). The caller supplies the absolute origin, which is now a **safety-critical** input (R7), authoritatively sourced and fail-closed when unprovable — not "accuracy-only."
- *Is `chunk_start_abs` precision accuracy-only or safety-critical?* → Safety-critical (R7). An early origin shifts geometry lookups earlier and can leak a sensitive frame; the coverage gate does not catch lookup misalignment. Verified the PTS clamp (`engine/video.py:160-164`) does not accumulate idle-gap skew (PTS is computed from absolute elapsed time), so the residual risk is the origin, handled by authoritative sourcing + fail-closed.
- *Fix 3: extend `.scrub_complete` or a separate record?* → Separate masked-video provenance record (masking runs after the scrub sentinel and may re-run independently). The fingerprint composition is resolved (R4): source-chunk bytes + independent geometry hash + policy fingerprint + `pixel_ratio` + frozen flag + `MASK_PROVENANCE_VERSION`.
- *Should the origin/last-chunk-extension reconciliation be deferred?* → No — it is promoted into U2 as a shared helper (R3), because changing only the terminal copy while `recovery` keeps its own `last_ts + 1.0` rule is what *creates* the drift the reconciliation prevents.

### Deferred to Implementation

- **Authoritative source of each chunk's absolute first-frame wall-clock (`chunk_start_abs`) on the terminal path.** The live path has it exact (`chunk_start_time`); the terminal path does NOT — and the deepening review found there is no persisted per-chunk first-frame timestamp for chunks 1..N today (only the recording-level `video_start_time` for chunk 0), while the obvious source (`base + idx·chunk_dur`) is unsafe (drifts early, R7's leak direction). The *whether* is settled (R7: authoritative-or-fail-closed); what's deferred is the concrete mechanism — derive a per-chunk first-frame origin, or rely on the fail-closed decoded-frame-extent bracketing check as the sole backstop. Confirm during U2 whether deriving a trustworthy per-chunk origin is feasible from on-disk data or whether bracketing-only is the accepted posture for the flag-ON world.
- **Whether to extract the FULL per-chunk `derive_chunk_ranges` helper** (beyond the origin + last-chunk-extension reconciliation that R3 puts in-scope). The fuller helper would also unify manifest/event range generation; evaluate during U2 and route to follow-up if it grows the unit.
- **Probe vs. two-pass decode for the PTS extent.** Prefer a cheap demux (packet PTS, no decode) before the transcode pass; confirm PyAV gives a reliable max PTS for action-gated VFR chunks during implementation.
- **Concrete geometry-bracketing check for R7's fail-closed-on-unprovable-origin.** How to assert the recorded geometry samples actually bracket the decoded frame extent (so a mis-origined chunk fails closed) — resolve against `_build_coverage`'s sample list during U2.

---

## High-Level Technical Design

> *This illustrates the intended approach and is directional guidance for review, not implementation specification. The implementing agent should treat it as context, not code to reproduce.*

Cloud chunk-video upload decision (flag ON), with the three new gates marked **[Fx]**:

```
recording (frozen masked_video_upload = ON)
        │
        ▼
run_terminal_stage (holds terminal flock for the whole critical section)
   ├─ FAST PATH: finalize_gate_satisfied AND masked provenance version current? [F3/R8]
   │     yes → sentinel + retention (skip produce)   |   no → fall through to produce
        │
        ▼
CloudCopyProducer.produce()
   recovery → scrub(.scrub_complete) → _mask_videos()           ── per chunk ──┐
                                                                                │
   reconcile masked_video/ as a CLOSED SET vs expected chunks [F3/H2]:          │
        purge any chunk_*.mp4 not produced/validated this pass (orphan/FAILED)  │
   for each expected chunk:                                                     │
     mask_chunk_for_cloud → mask_video_chunk_for_cloud → mask_video_chunk()     │
        ├─ authoritative chunk_start_abs (shared range helper w/ recovery) [F2/R3,R7]
        ├─ probe PTS extent  →  end_ts = chunk_start_abs + last_pts   [F2]      │
        ├─ verify geometry brackets the decoded frames, else FAIL  [F2/R7]      │
        ├─ _build_coverage([start_ts, end_ts])  → (a) safe / (b) FAIL / (c) mask│
        ├─ _transcode: any frame abs_ts ∉ [start,end] → FAIL  [F2]              │
        └─ on MASKED/SAFE → write masked_video/chunk_NNNN.mp4                    │
            on FAILED/mismatch → purge any stale masked_video/chunk_NNNN  [F3]  │
   (same masker authority + guard applies to the LIVE caller) [F2/R9] ─────────│
                                                                                │
   after full pass → write masked_video/.masked_provenance.json     [F3] ◄──────┘
        │
        ▼
upload enqueue — THREE enumeration paths, each gated [F1/R1]:
   (a) live _cloud_upload_chunk / terminal _upload_source_chunk_media
          → upload_chunk_files → FileInfo → assert_uploadable + assert_video_masked
   (b) reconcile path → its own FileInfo → assert_uploadable + assert_video_masked
   (c) terminal upload_recording(scrubbed_dir) → list_recording_files
          → assert_uploadable + assert_video_masked   ◄── today UNGATED (H3)
        │   assert_video_masked: flag ON ⇒ chunk_*.mp4 path MUST be under masked_video/ else RAISE
        ▼
   request_signed_urls → upload   (all under the one flock hold; no masked_video/ mutation between validate & upload)

reuse path: is_scrubbed_copy_reusable(.scrub_complete) AND masked provenance valid [F3]
            → reuse masked_video/ ; else purge closed-set + re-mask
```

---

## Implementation Units

### U1. Upload-seam masked-video assertion (Fix 1)

**Goal:** Add a fail-closed hard gate so that, when the frozen `masked_video_upload` decision is ON, no `chunk_*.mp4` can be enqueued for cloud upload unless its local path is the masker's output — applied at *every* enumeration/enqueue path, including the scrubbed-dir rglob that bypasses `upload_chunk_files`.

**Requirements:** R1, R6

**Dependencies:** None (independent defense-in-depth; does not require U2/U3).

**Files:**
- Modify: `src/screencap/upload.py` (add `assert_video_masked` next to `assert_uploadable`; extend the R8 invariant comment block; invoke it in `list_recording_files`/`upload_recording` so the scrubbed-dir rglob path is gated — resolving the frozen flag from the recording dir)
- Modify: `src/screencap/chunk_processor.py` (invoke the gate at each `assert_uploadable` site — `upload_chunk_files` ~`1290` and the reconcile path ~`382`; resolve the frozen flag once and pass it down)
- Test: `tests/test_upload.py` (gate unit behavior + scrubbed-dir enumeration), `tests/test_pipeline_chunk_ops.py` or `tests/test_unified_upload_pipeline.py` (wired behavior across all enqueue paths)

**Approach:**
- `assert_video_masked(file_info, *, masked_upload_on)` returns `file_info` unchanged when uploadable; raises `ValueError` when `masked_upload_on` and `file_info.name` matches `chunk_*.mp4` and `file_info.path` is not under a `masked_video/` directory. Mirror `assert_uploadable`'s loud-failure docstring and the denylist comment style.
- Apply at **all three** enumeration paths (deepening H3 + reconcile finding): (a) the `upload_chunk_files` `FileInfo` construction (covers live `_cloud_upload_chunk` + terminal `_upload_source_chunk_media`); (b) the reconcile-path `FileInfo` construction (`chunk_processor.py:382`, which re-enumerates via `_collect_chunk_files` and can produce a masked-or-source slot); (c) the `list_recording_files`/`upload_recording` enumeration (`terminal_stage.py:848` → `upload.py:212`), the path that today ships `masked_video/chunk_NNNN.mp4` under a nested key with no masked-video check and would ship a stray `chunk_*.mp4` at the scrubbed-dir root ungated.
- Path-structural is a *sufficient* proxy here when paired with U3: a clear copy can only legitimately sit under `masked_video/` as an `UNMASKED_PROVABLY_SAFE` output (coverage-proven), and any foreign/stale copy under `masked_video/` is U3's provenance problem, not U1's. U1 deliberately does not content-inspect.
- Resolve `masked_upload_on` once in the **caller** and pass it as a parameter into the gate; never read the mutable global in the gate. Design decision (resolves a cross-persona finding): for the `list_recording_files`/`upload_recording` seam, the *caller* (`terminal_stage` at the `upload_recording(scrubbed_dir)` site, `terminal_stage.py:848`) resolves the bool via `get_frozen_masked_video_upload(self._recording_dir)` (the SOURCE recording dir) and passes it into `upload_recording`/`list_recording_files` as a parameter — a pre-flight resolution that keeps `upload.py` free of a `catalog`/`.recording_intent` dependency. Do NOT have `upload.py` derive the source dir from the scrubbed-dir name. (Note: the scrubbed dir DOES carry a *copied* `.recording_intent` — `copytree` includes it and `_SOURCE_HASH_GLOBS` hashes it — so resolving from the scrubbed dir would also work, but pre-flight resolution from the source dir is the chosen, dependency-clean design.)
- **Absent-signal hardening (fail-closed, R6):** `get_frozen_masked_video_upload` falls back to the mutable global when `.recording_intent` is missing/corrupt/legacy. For the UPLOAD GATE specifically, an *absent/unreadable* frozen decision must NOT inherit a global-ON to wave a source chunk through, nor inherit a global-OFF to skip the check — the gate treats an unresolvable frozen decision as "cannot prove masked" and fails closed (reject the `chunk_*.mp4` unless it is under `masked_video/`). See the frozen-resolution hardening in Key Technical Decisions.
- Non-video slots (audio/events/manifest/transcript) are never `chunk_*.mp4`, so the matcher is scoped to the video name only.

**Patterns to follow:** `upload.assert_uploadable` / `_is_raw_artifact` (`upload.py:181-209`); the masked-path switch in `chunk_processor._collect_chunk_files` (`chunk_processor.py:1097-1104`) and `terminal_stage._upload_source_chunk_media` (`terminal_stage.py:1003-1012`).

**Test scenarios:**
- Happy path — flag OFF: a source `chunk_0000.mp4` (path under the recording dir) passes the gate unchanged (today's behavior preserved).
- Happy path — flag ON, masked path: `chunk_0000.mp4` whose path is `<name>-scrubbed/masked_video/chunk_0000.mp4` passes.
- Error path (the core fix) — flag ON, source path: `chunk_0000.mp4` whose path is the rich source dir RAISES; not enqueued (fail closed). Covers R1/R6.
- Error path (H3) — flag ON, scrubbed-dir rglob: a `chunk_0000.mp4` planted at the scrubbed-dir *root* (outside `masked_video/`) is rejected by `upload_recording`/`list_recording_files`; it never reaches `request_signed_urls`. Covers R1.
- Edge case — non-video slots: `audio_0000.flac`, `events_0000.jsonl`, `chunk_0000_manifest.json` pass regardless of flag state.
- Integration — wired, masked copy missing: with frozen flag ON and no masked copy, a chunk upload attempt fails closed at the seam (no silent fall-back to source), exercised through `upload_chunk_files`.
- Integration — parity across all three paths: a flag-ON enqueue from `_cloud_upload_chunk`, `_upload_source_chunk_media`, and `upload_recording(scrubbed_dir)` each hit the gate.

**Verification:** With the frozen flag ON, no enumeration path (chunk-media enqueue, reconcile, or scrubbed-dir rglob) can enqueue a `chunk_*.mp4` outside `masked_video/`; an attempt raises and is logged. Flag-OFF behavior is byte-for-byte unchanged. `tests/test_upload.py` and the wired tests pass.

---

### U2. Coverage gate over the chunk's real decoded frame extent (Fix 2)

**Goal:** Eliminate the trailing-clear-frame leak AND the interior-misalignment leak: prove coverage over each chunk's actual decoded PTS extent, align every per-frame geometry lookup to true wall-clock (fail closed when the absolute origin can't be proven), fail closed on any frame outside the proven span, and let legitimate VFR/last chunks (terminal *and* live) pass instead of spuriously failing.

**Requirements:** R2, R3, R6, R7, R9

**Dependencies:** None for landing; the highest-risk unit. Shares `pipeline_chunk_ops`/`terminal_stage`/`recovery` edits with U1/U3 — execute serially. U2 owns the `MASK_PROVENANCE_VERSION` bump that U3 consumes.

**Files:**
- Modify: `src/screencap/video_mask.py` (probe the chunk's PTS extent to derive `end_ts`; thread `start_ts`/`end_ts` into `_transcode_with_masks`; fail closed on any out-of-span decoded frame; add the origin/geometry-bracketing fail-closed check for R7)
- Modify: `src/screencap/terminal_stage.py` (`_mask_videos` / `_chunk_timing`: supply an *authoritative* absolute origin, stop using uniform `chunk_dur` for the span end)
- Modify: `src/screencap/recovery.py` (`_recover_chunk_metadata` chunk-range loop `128-134`: consume the shared origin/last-chunk-extension helper so manifests/events and the masked video reason over the same boundaries — R3)
- Create/Modify: a shared range-derivation helper (origin + `last_ts + 1.0` last-chunk extension) consumed by both `terminal_stage` and `recovery` (location TBD — likely `recovery.py` or a small shared module)
- Modify: `src/screencap/pipeline_chunk_ops.py` (`mask_chunk_for_cloud` signature/contract; keep the live caller `chunk_processor.py:1153` working under the new masker authority)
- Modify: `src/screencap/scrubber.py` (add `MASK_PROVENANCE_VERSION` constant; bump it because masking behavior changes — U3 consumes it)
- Test: `tests/test_video_mask.py` (extent + guard + origin-misalignment), `tests/test_terminal_stage.py` (caller span derivation + shared helper), `tests/test_pipeline_chunk_ops.py` (live-caller parity through the guard)

**Approach:**
- **Real extent:** before the coverage gate, derive the chunk's real frame extent from its own PTS (cheap demux probe; first packet ≈ 0). Take `max(packet.pts)` over ALL demuxed packets (skipping `None`), NOT the last packet's PTS — VFR packet/decode order is not guaranteed PTS-monotonic, so last-packet PTS can under-report the extent and re-open the trailing-frame leak. Set `end_ts = chunk_start_abs + max_pts_seconds` so `_build_coverage` proves geometry density out to the true last frame; if geometry doesn't reach it, the three-way gate fails closed. The in-loop out-of-span guard remains the authoritative fail-closed backstop if the probe under-reports.
- **Out-of-span guard:** in `_transcode_with_masks`, track each decoded frame's `abs_ts`; any frame outside `[start_ts - eps, end_ts + eps]` → raise → FAILED, drop the copy. With masker-derived `end_ts` this is an invariant check that makes a future caller miscalculation safe, not silent.
- **Wall-clock alignment (R7 — the deepening-review safety correction):** the per-frame lookup `abs_ts = chunk_start_abs + frame_pts·time_base` is only correct if `chunk_start_abs` is the true wall-clock of PTS 0. An origin *earlier* than truth shifts lookups earlier and can mask a window from before a sensitive window opened → leak (the coverage gate does NOT catch this — it proves density in DB-time, not lookup alignment). Verified the capture encoder computes PTS from absolute elapsed time per frame (`engine/video.py:160`), so idle gaps do NOT accumulate skew and the monotonic clamp (`:163`) only nudges sub-frame-interval bursts forward (over-mask, safe) — the residual risk is purely the absolute origin.
  - **Sharp edge (deepening):** the *obvious* terminal-path source is the existing uniform grid `base + idx·chunk_dur` (`terminal_stage.py:480`), and for chunks 1..N that grid value can land EARLIER than the chunk's true first-frame wall-clock — exactly R7's unsafe direction. Worse, the only authoritative per-chunk first-frame timestamp persisted today is the recording-level `video_start_time` (chunk 0 only); chunks 1..N have no persisted first-frame wall-clock. So R7 cannot be satisfied merely by "picking an on-disk source" — either a per-chunk first-frame timestamp must be derived (e.g., from the chunk's own PTS-0 mapped through a trustworthy origin) or the **fail-closed geometry-bracketing check becomes the sole runtime backstop** and MUST bracket the *decoded-frame extent* (not the grid span). U2 must implement that bracketing check, not rely on the origin being right.
  - **R3↔R7 tension to resolve in U2:** the R3 shared helper reconciles the masker with recovery's *grid* boundaries (so manifests/events and the masked video agree). But R7's safety origin may be the chunk's true first frame, not the grid value. Resolve by scoping the shared helper to manifest/event boundary reconciliation (last-chunk extension + grid) while the masker's safety-critical origin/bracketing is sourced independently and fails closed — do NOT bake the grid origin into the masker's lookup as if it were authoritative.
- **Live/terminal parity (R9):** U2 makes `mask_video_chunk` authoritative for the span; the live caller's exact `rotation_time` (which overshoots the last decoded frame) becomes advisory. The live path is therefore subjected to the same guard — prove it does not spuriously FAIL honest VFR live chunks (last-frame PTS < `rotation_time` is normal and must pass). Drop the prior "live path unaffected" assumption.
- **Shared range reconciliation (R3):** promote the origin + `last_ts + 1.0` last-chunk-extension derivation into a shared helper used by both `recovery._recover_chunk_metadata` and the terminal masker path, so the masked video and recovery-generated manifests/events cannot diverge on chunk boundaries. (Changing only the terminal copy while recovery keeps its own rule is what *creates* drift.)
- Handle the action-empty / near-zero-span chunk as a legitimate value (proves safe / masks / fails closed; no crash, no over-wide span). Preserve VFR encoder fidelity (`out_stream.time_base = time_base`, `bf=0`, never override `packet.pts` after encode).

**Execution note:** Characterization-first — capture both the current `MASKED`-with-trailing-clear-frames behavior AND a mis-origined-chunk-masks-wrong-window behavior in failing tests before changing the logic, so each fix is proven to flip a real leak to fail-closed/masked.

**Patterns to follow:** the three-way gate semantics in `video_mask.mask_video_chunk` (`341-498`); the atomic temp+replace whole-chunk gate in `_transcode_with_masks` (`501-617`); recovery's last-chunk `last_ts + 1.0` extension (`recovery.py:133`); `SCRUB_PROVENANCE_VERSION` as the model for `MASK_PROVENANCE_VERSION` (`scrubber.py:65`).

**Test scenarios:**
- Happy path — uniform chunk, dense geometry, no sensitive window: `UNMASKED_PROVABLY_SAFE`, frame count equals decoded frames, no frame outside span.
- Happy path — sensitive window across the whole real extent: `MASKED`, `regions > 0`, every decoded frame (including those beyond the old uniform `chunk_dur`) carries a mask. Covers R2/R3.
- **Adversarial EFFECT test (trailing-frame leak):** a chunk whose real PTS extent exceeds the nominal span, with a sensitive window active in the *trailing* region. Decode the output and assert those frames are masked (region filled, not source pixels) or the chunk is `FAILED` — never a `MASKED` chunk with trailing clear frames. Covers R2/R6 and the "no-op looks like success" learning.
- **Adversarial EFFECT test (interior misalignment, R7):** a chunk whose supplied `chunk_start_abs` is earlier than the true first-frame wall-clock, with a sensitive window that opens only in the later portion. Assert the late frames are masked or the chunk `FAILED` — it must NOT mask the earlier (wrong) window and ship the sensitive frame clear. Covers R7.
- Error path — out-of-span guard: inject a frame whose `abs_ts` exceeds `end_ts` (deliberately too-short caller span) → raise → `FAILED`, no copy.
- Error path — origin unprovable: a chunk whose geometry samples do not bracket the decoded frame extent → `FAILED` (R7), no copy.
- Error path — geometry too sparse to reach the real end: real extent past the last geometry sample by > `MAX_GEOMETRY_GAP_SECONDS` → `FAILED` (case b), no copy.
- Edge case — action-empty / near-zero-span chunk: legitimate value (proves safe / masks / fails closed), no crash, no over-wide span.
- Edge case — PTS starts where expected: assert the derived span uses real decoded packet PTS (not `last_pts > 0` only), guarding the corruption class from the PTS learning.
- Integration — terminal caller: `_mask_videos` over a multi-chunk recording where the last chunk is longer than `chunk_dur` → fully-masked (or FAILED) last chunk, never `MASKED`-with-trailing-clear.
- Integration — LIVE caller parity (R9): `mask_chunk_for_cloud` from `chunk_processor.py:1153` with action-gated VFR input (last frame PTS < `rotation_time`) passes the guard (no spurious FAILED) and masks correctly.
- Integration — recovery reconciliation (R3): recovery-generated manifests/events and the masker use the same chunk boundaries via the shared helper (assert identical `(start, end)` for each chunk).

**Verification:** No frame is encoded into a cloud copy outside a coverage-proven span; no chunk masks the wrong wall-clock window. A chunk longer than the nominal span is masked end-to-end or FAILED. Both adversarial EFFECT tests fail on pre-fix code and pass after. The live caller is exercised through the guard and does not spuriously FAIL. `MASK_PROVENANCE_VERSION` is bumped. `tests/test_video_mask.py`, `tests/test_terminal_stage.py`, `tests/test_pipeline_chunk_ops.py` pass.

---

### U3. Masked-video provenance in the scrubbed-copy reuse path (Fix 3)

**Goal:** Ensure no `produce`/convergence path can ship a stale or under-masked `masked_video/` copy: validate masked-video provenance before trusting masked output, reconcile `masked_video/` as a closed set on every pass (purging orphans/FAILED/mismatches), and make the convergence fast path provenance-version-aware.

**Requirements:** R4, R6, R8

**Dependencies:** Consumes U2's `MASK_PROVENANCE_VERSION` and FAILED semantics; sequence after U2. Shares `terminal_stage.py`/`scrubber.py` edits with U1/U2 — run serially.

**Files:**
- Modify: `src/screencap/scrubber.py` (masked-video provenance writer + validator alongside `_write_scrub_sentinel`/`is_scrubbed_copy_reusable`/`_compute_source_hash`; on coverage-FAILED in `mask_video_chunk_for_cloud`, remove any pre-existing `output_path`)
- Modify: `src/screencap/terminal_stage.py` (`CloudCopyProducer.produce` / `_mask_videos`: closed-set reconcile + purge of `masked_video/`; validate provenance before trusting it; write provenance after a fully-successful pass; make the `finalize_gate_satisfied` fast path — `terminal_stage.py:810-820` — verify masked provenance version before declaring convergence)
- Test: `tests/test_scrubber_class.py` (provenance write/validate + stale-copy purge), `tests/test_terminal_stage.py` (produce reuse-vs-rebuild + closed-set purge + fast-path version check)

**Approach:**
- **Provenance record (explicit fingerprint, R4/H5):** write a dot-prefixed `masked_video/.masked_provenance.json` (kept out of the upload set by the dotfile filter) atomically (temp + `os.replace`, mode 0600; use a pid+uuid temp name and sweep stale `.masked_provenance.json.*.tmp` on write, mirroring `_transcode_with_masks`'s orphan-sweep at `video_mask.py:524`, so a crash-orphaned temp is reclaimed). Write it after a whole pass that produced/validated every expected chunk. It records, explicitly: per-source-chunk byte fingerprint (size + content hash of source `chunk_NNNN.mp4` — the input `_SOURCE_HASH_GLOBS` omits), an **independent geometry hash scoped to the geometry-only tables** (e.g. `window_geometry`), NOT a hash of the whole `recording.db` — the DB also holds the `pipeline_chunk_state` ledger, which mutates throughout/after masking (`mark_scrubbed`/`mark_failed` and later upload-confirm transitions), so a full-DB hash would change on every ledger write and make reuse spuriously refuse forever (fail-closed but defeats the R4 reuse optimization entirely); the geometry-table scope is stable across ledger churn while still catching a geometry re-insert. Also records the **privacy-policy fingerprint** (privacy mode + masking-relevant config), **`pixel_ratio`** (changes every mask rect's geometry), the frozen flag, and `MASK_PROVENANCE_VERSION` (from U2).
- **Closed-set reconciliation (H2 — the core hole, and the AUTHORITATIVE purge):** the only wholesale `masked_video/` rebuild is inside `scrub_recording` (rmtree+copytree), which the reuse path SKIPS; a case-(b) FAILED leaves a pre-existing `output_path` intact; an orphaned index (source chunk gone) is never visited by `_mask_videos`. So on every `produce`, reconcile `masked_video/` against the current expected chunk set and **purge any `masked_video/chunk_*.mp4` not produced/validated by the current pass** — covering the reuse path and orphaned indices, not just remove-on-FAILED. The `produce`-level reconciler is the *authoritative* mechanism that alone closes the hole; the per-chunk in-masker FAILED purge below is a secondary belt. A partial landing must keep the reconciler (omitting it re-opens the reuse-path orphan hole even if the in-masker purge lands); the in-masker purge alone is insufficient because it never runs for a skipped/orphaned index.
- **Reuse decision:** trust an existing `masked_video/` only when provenance is present, complete (one entry per expected chunk), `MASK_PROVENANCE_VERSION` matches, and every fingerprint (source bytes, geometry, policy, pixel_ratio) matches current inputs. Any miss → purge the closed set and re-mask. Closes the hole AND enables a safe skip (no redundant re-encode when nothing changed).
- **Convergence fast path (H4/R8):** the `finalize_gate_satisfied` early-return skips `produce` entirely, so a recording converged under an older `MASK_PROVENANCE_VERSION` would never be re-masked. The fast path must verify the masked provenance version (and presence) before declaring convergence; on a version miss it falls through to `produce` (re-mask) rather than writing the sentinel.
- **Stale-copy invariant:** on coverage-FAILED (or mismatch), no pre-existing `masked_video/chunk_NNNN.mp4` survives (today `mask_video_chunk` leaves it untouched on case-(b) FAILED).
- **TOCTOU:** validate-and-upload occur under one continuous terminal-flock hold (already true); assert `masked_video/` is not mutated between provenance-validate and the step-3/step-3b uploads.

**Execution note:** Test-first for the stale-copy + orphan invariants — plant a stale `masked_video/chunk_0007.mp4` whose source chunk is absent on a reuse-path produce and assert it is purged and never enqueued, before adding the reconciler.

**Patterns to follow:** `_write_scrub_sentinel`/`is_scrubbed_copy_reusable`/`_compute_source_hash` (`scrubber.py:3011-3101`); the dotfile-exclusion convention (`.scrub_complete`, `.video_review.mp4`); atomic temp+replace with `O_NOFOLLOW`/0600; the closed-set seeding discipline from `docs/solutions/runtime-errors/chunk-upload-sentinel-gating-and-data-loss.md`.

**Test scenarios:**
- Happy path — fresh pass writes provenance with one entry per expected chunk; a subsequent produce with unchanged inputs validates and reuses (no re-encode).
- Error path (core fix) — stale `masked_video/chunk_0000.mp4` + provenance whose source-chunk fingerprint or `MASK_PROVENANCE_VERSION` mismatches: reuse refused, closed set purged, re-mask runs; stale bytes never enqueued. Covers R4/R6.
- Error path (H2 orphan) — `masked_video/chunk_0007.mp4` whose source `chunk_0007.mp4` is gone on a reuse-path produce (so `_mask_videos` never visits index 7): the closed-set reconcile purges it; it never enters the rglob upload set. Covers R4.
- Error path — coverage-FAILED chunk with a pre-existing masked copy: the FAILED path removes it; no `masked_video/chunk_NNNN.mp4` survives for that index.
- Error path (H4/R8) — converged recording + bumped `MASK_PROVENANCE_VERSION`: the fast path does NOT take the no-op early return; it falls through and re-masks.
- Edge case — incomplete provenance (missing an expected chunk entry): not-reusable → re-mask.
- Edge case — policy fingerprint changed while source/geometry unchanged: reuse refused → re-mask (input `_SOURCE_HASH_GLOBS` omits).
- Edge case — `pixel_ratio` changed while source/geometry/policy unchanged: reuse refused → re-mask.
- Edge case — ledger churn between mask and reuse: the `pipeline_chunk_state` ledger in `recording.db` advances (`mark_scrubbed` → upload-confirm) after masking, but the geometry-only hash is unchanged → reuse remains valid (no spurious re-mask). Guards the R4 reuse optimization against the full-DB-hash pitfall.
- Integration — `CloudCopyProducer.produce` reuses a valid masked set on the second call and rebuilds after an input change, ledger reflecting per-chunk outcomes.

**Verification:** No `produce` or convergence path can upload a `masked_video/` copy whose provenance does not match current source + geometry + policy + pixel_ratio + mask-version; orphaned/FAILED/stale copies are purged as a closed set; the fast path re-masks on a version bump. `tests/test_scrubber_class.py` and `tests/test_terminal_stage.py` pass.

---

### U4. SECURITY.md sync (Doc)

**Goal:** Keep the masked-video trust-boundary documentation accurate after the hardening; confirm the flag default stays OFF and the enablement prerequisites remain documented.

**Requirements:** R5

**Dependencies:** U1, U2, U3 (documents their landed behavior).

**Files:**
- Modify: `SECURITY.md` (the "Cloud recording privacy: capture-time blocking vs. post-hoc masking" section, lines ~32-47)

**Approach:**
- Update the fail-closed-gate description to reflect: (1) the explicit upload-seam masked assertion (U1) applied at *all* enumeration paths, (2) coverage proven over the chunk's real decoded extent AND wall-clock-aligned per-frame lookup (U2/R7), and (3) reuse + convergence gated on masked-video provenance with closed-set reconciliation (U3).
- Note the accepted residual: the convergence fast path re-masks on a `MASK_PROVENANCE_VERSION` bump (R8), so the operational guarantee tracks the current mask logic; record any remaining limitation explicitly rather than implying perfection.
- Keep the two enablement prerequisites and the default-OFF statement. Adjust prerequisite wording only where this work changes the picture (the live-upload cutover already landed; the consent-surface reconciliation remains outstanding and deferred).
- Do not overstate: post-hoc masking remains an *operational* guarantee weaker than capture-time blocking; this work hardens it, it does not make it structural.

**Test scenarios:** Test expectation: none — documentation-only change, no behavioral code.

**Verification:** `SECURITY.md` accurately describes the three hardened gates and still states the flag is OFF by default with the consent-surface prerequisite outstanding. No claim contradicts the shipped code.

---

## System-Wide Impact

- **Interaction graph:** There are THREE upload enumeration paths, not one chokepoint (deepening correction): (a) `upload_chunk_files` (live `_cloud_upload_chunk` + terminal `_upload_source_chunk_media`), (b) the reconcile-path `FileInfo` construction (`chunk_processor.py:382`), (c) the scrubbed-dir rglob `upload_recording`→`list_recording_files` (`terminal_stage.py:848`). U1's gate must sit at all three. U2 makes the masker authoritative for the span on BOTH the terminal (`_mask_videos`) and live (`_cloud_upload_chunk`→`mask_chunk_for_cloud`) callers — the live path is a U2 consumer subject to the new guard, NOT unaffected. U3 plugs into `CloudCopyProducer.produce`'s reuse decision AND the `finalize_gate_satisfied` fast path (`terminal_stage.py:810-820`).
- **Error propagation:** Every new gate fails CLOSED — raise/FAILED, never a clear copy. A U2 FAILED marks the chunk `mark_failed` in the ledger (blocks the completeness sentinel and eviction) — note this is a *behavior change reachable on the live path* once the flag flips, so the guard must not fire on honest VFR input (R9). A U1 raise aborts that chunk's enqueue and is logged. A U3 mismatch purges the closed set + re-masks.
- **State lifecycle risks:** U3's closed-set purge is the key partial-state concern — a prior run's masked copy must not outlive a FAILED/mismatched/orphaned re-run, including on the reuse path (which skips `scrub_recording`'s wholesale rebuild) and for indices the current pass never visits. Provenance is written only after a complete pass (closed-set), mirroring the sentinel-last rule; validate-and-upload stay under one flock hold.
- **API surface parity:** The live and terminal paths must enforce the same masked invariant (U1) and the same span authority + guard (U2/R9). Do not add either to only one path; the live caller needs an explicit guard test.
- **Integration coverage:** Needs decode-the-output EFFECT tests for BOTH the trailing-frame and interior-misalignment leaks (U2), a live-caller parity test through the guard (U2/R9), and reuse-vs-rebuild + orphan-purge + fast-path-version produce() tests (U3) — all using real objects, not mocks; unit assertions on the labeller alone miss exactly these fail-open modes.
- **Unchanged invariants:** Flag default stays OFF; capture-time blocking and `RecorderPrivacyFilter` are untouched; `recording.db` R8 local-only rule and `assert_uploadable` are unchanged (the new gate is additive); GCS object keys (`chunk_NNNN.mp4`) are unchanged.

---

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| A subtle span-derivation bug spuriously fails legitimate chunks closed (no masked upload). | Safe direction (no leak); covered by happy-path + VFR-last-chunk + live-caller-parity tests so over-conservatism is caught, not shipped. Flag is OFF so no production impact during development. |
| The in-masker out-of-span guard fires on honest VFR jitter (float rounding) or honest live input where last-frame PTS < `rotation_time`. | Epsilon tolerance on the boundary; masker-derived `end_ts` makes the guard an invariant check, not the primary span source; explicit live-caller parity test (R9) proves no spurious FAILED. |
| `chunk_start_abs` origin earlier than truth shifts geometry lookups earlier → masks the wrong window → **leak** (the interior-misalignment hole). | Treated as a SAFETY property (R7), not accuracy-only: authoritative origin sourcing + fail-closed when geometry cannot be proven to bracket the decoded frames; adversarial EFFECT test for a mis-origined chunk. |
| Provenance fingerprint misses a masking-relevant input (policy, geometry, pixel_ratio), allowing stale reuse. | Fingerprint explicitly includes source-chunk bytes + independent geometry hash + policy + `pixel_ratio` + `MASK_PROVENANCE_VERSION`; default-deny (any miss → purge + re-mask); tests cover policy-only and pixel_ratio-only changes. |
| A stale `masked_video/` copy survives the reuse path or an orphaned index and is rglob-uploaded. | Closed-set reconciliation purges any `masked_video/chunk_*.mp4` not produced/validated by the current pass; H3 gate also rejects an out-of-`masked_video/` chunk at the rglob seam. |
| A recording converged under old mask logic is never re-masked when `MASK_PROVENANCE_VERSION` bumps. | Fast-path (`finalize_gate_satisfied`) is made provenance-version-aware (R8): a version miss falls through to `produce` and re-masks. |
| `get_frozen_masked_video_upload` falls back to the mutable global on a missing/corrupt `.recording_intent`, so once the global is ON a recording with unreadable intent could be governed by the global rather than its capture-time posture (fail-open at the resolution layer). | The upload-seam gate (U1) treats an unresolvable frozen decision as fail-closed (reject a non-`masked_video/` chunk), independent of the global; documented in Key Technical Decisions. |
| A full-`recording.db` hash in the masked-video provenance changes on every ledger write, making reuse refuse forever (defeats R4's reuse optimization). | Scope the independent geometry hash to geometry-only tables (`window_geometry`), stable across `pipeline_chunk_state` ledger churn. |
| PyAV demux does not give a reliable max PTS for action-gated VFR. | Deferred-to-implementation probe validation; fallback is the in-loop guard (still fail-closed) and the best-effort `_chunk_timing` posture. |
| Edits to `terminal_stage.py`/`scrubber.py`/`pipeline_chunk_ops.py`/`recovery.py` shared by U1–U3 cause merge/index contention. | Execute serially (U1 → U2 → U3 → U4), not in parallel; each lands as its own commit. |

---

## Documentation / Operational Notes

- Flag stays OFF; no rollout, migration, or monitoring change. The only operator-visible artifact is `SECURITY.md` (U4).
- After merge, capture the three `docs/solutions/` learning gaps (masked-video coverage-gate design, recovery↔terminal range reconciliation, scrubbed-copy reuse/provenance) via `/ce-compound` — flagged by the learnings search as genuinely novel territory.

---

## Sources & References

- Linear: [SCR-126](https://linear.app/zk-email/issue/SCR-126/masked-video-upload-flag-enablement-prerequisites-and-fail-open) (blocked-by SCR-125, merged)
- Trust boundary: `SECURITY.md` (masked-video section)
- Related origin (deferred prereq #2): `docs/brainstorms/2026-06-03-native-redaction-review-before-upload-requirements.md`
- Pipeline plan: `docs/plans/2026-06-09-001-refactor-scr-125-unified-per-chunk-upload-plan.md`
- Learnings: `docs/solutions/integration-issues/mitmproxy-ignore-hosts-tunneling-all-flows-2026-04-29.md`, `docs/solutions/runtime-errors/chunk-upload-sentinel-gating-and-data-loss.md`, `docs/solutions/runtime-errors/capture-health-nonscreen-attribution-terminal-stop-2026-06-01.md`, `docs/solutions/bug-fixes/video-pts-offset-bframe-corruption-20260322.md`, `docs/solutions/integration-issues/review-data-nullable-timing-swift-consumer-2026-06-01.md`
- Key code: `src/screencap/upload.py`, `src/screencap/video_mask.py`, `src/screencap/terminal_stage.py`, `src/screencap/scrubber.py`, `src/screencap/pipeline_chunk_ops.py`, `src/screencap/chunk_processor.py`, `src/screencap/recovery.py`, `src/screencap/config.py:381`
