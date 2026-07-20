---
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
execution: code
product_contract_source: ce-plan-bootstrap
title: "feat: Cloud Intelligence tasks run by default; day-split uses any connected model"
date: 2026-07-16
type: feat
plan_depth: deep
related_tickets:
  - SCR-272 (deferred point 3 — frames toggle brainstorm)
---

# feat: Cloud Intelligence tasks run by default; day-split uses any connected model

## Summary

Reframe the Intelligence pane's "WHAT CLOUD MODELS MAY DO" section from a **control panel** (per-task cloud toggles the user must flip) into a **transparency panel** (a description of what a connected cloud model does with your data). Two product decisions drive it:

1. **Cloud tasks run by default** — connecting a cloud provider *is* the consent; users don't toggle individual jobs. Summaries, titles, and answers use your connected model automatically (as the fallback when on-device is unavailable), sending text only.
2. **Day-splitting is never lost** — when there's no on-device model but a cloud model is connected, the day is split and labeled by that cloud model instead of degrading to mechanical idle-gap names.

Point 3 (making "Screen frames or images" a real toggle) is **out of scope** and tracked for brainstorming in **[SCR-272](https://linear.app/zk-email/issue/SCR-272/brainstorm-should-screen-frames-or-images-become-a-user-facing-toggle)**. The frames-never-sent guarantee is untouched here.

The key implementation finding: the cloud day-split/label path (`terminal_stage._summary_cloud_fallback` → `cloud.segment()`, which returns full day boundaries + labels) **already exists and is tested**. It is gated only by `summary_cloud_consent` defaulting OFF. So both product decisions are delivered primarily by **flipping two config-getter defaults** — the `resolve()` matrix and the terminal-stage wiring stay structurally intact — plus the Swift UI reframe.

---

## Problem Frame

Today the section shows two real toggles (`summary_cloud_consent`, `recall_cloud_consent`), a fixed "On-device" day-split row, and a fixed "Always off" frames row, plus a standing nudge telling users to turn the toggles on.

Two problems:

- **The toggles are friction the user doesn't want.** They default OFF, so a user who connects a cloud provider still gets nothing from it until they hunt down and flip per-task switches. The product decision is that connecting a provider should be enough.
- **Day-splitting silently degrades.** `ConsentPolicy` pins `DAY_SPLIT` to on-device/heuristic. With no on-device model, the day is split by the mechanical idle-gap heuristic (`task_1`, `task_2` …) — effectively useless labels — unless the summary cloud fallback happens to be consented. Users perceive the feature as "lost."

Both are fixed by making the connected cloud provider the consent gate and letting the existing cloud fallback deliver day-split/labeling.

**Non-goal (this plan):** sending screen frames/images to any cloud model. That guard (`TaskKind.FRAMES → NEVER`) is preserved verbatim; the decision to revisit it is SCR-272.

---

## Requirements

- **R1 (point 1).** Summaries/titles and recall-answers use a connected cloud provider automatically — no per-task toggle. On-device stays preferred; cloud is the fallback only when on-device is unavailable **and** a cloud provider is configured.
- **R2 (point 2).** When on-device is unavailable and a cloud provider is configured, the day is split **and** labeled by that cloud model, not the mechanical heuristic. With no cloud provider, the heuristic still applies (day-split is never fully lost either way).
- **R3 (point 1 UI).** The two consent toggles and the standing nudge are removed from the Intelligence pane. The section becomes an honest description of what a connected cloud model does.
- **R4 (privacy invariant, unchanged).** Screen frames/images are never sent to any cloud model. Masked/blocked apps are stripped (text) before any provider — local or cloud — runs. Only the ALLOW-only, `stripped=True`-marked text summary/evidence ever reaches a cloud provider.
- **R5 (honest copy).** The frames row keeps its "Always off" guarantee; the day-split row no longer claims "never a cloud task"; all copy passes the honest-copy audit.
- **R6 (deferred).** Point 3 (frames toggle) is filed as SCR-272 and not implemented here.

---

## Key Technical Decisions

**KTD1 — Deliver the behavior by flipping the config-getter defaults, not by restructuring `resolve()`.**
`config.get_summary_cloud_consent()` and `config.get_recall_cloud_consent()` change default `False → True`. `ConsentPolicy.resolve()` already reads `cloud_provider is not None` as the hard precondition for CLOUD, so with consent defaulting on, the effective gate becomes "a cloud provider is configured" — exactly "connecting a provider is the consent." The preference order (on-device first, cloud as fallback) is untouched. *Rationale:* smallest honest change to a privacy-critical, heavily-tested module; preserves every fixed guard; keeps a power-user env/toml override (`SCREENCAP_SUMMARY_CLOUD_CONSENT=0`) for those who want to disable cloud fallback from the CLI.

**KTD2 — Day-split rides the existing `_summary_cloud_fallback`; the `DAY_SPLIT → never-cloud` guard stays.**
`ConsentPolicy.resolve(DAY_SPLIT)` continues to return only ON_DEVICE/HEURISTIC, and `degrade.resolve_day_split`'s defensive "never CLOUD" assertion is preserved. The user-visible cloud day-split is delivered by the SUMMARY cloud fallback in `terminal_stage`, which calls `cloud.segment(summary)` and gets back a full `{"tasks": […]}` (boundaries **and** labels). Under KTD1 that fallback now runs whenever a cloud provider is configured, before the mechanical heuristic. *Rationale:* reuses the single sanctioned consented-cloud dispatch pattern; avoids inverting a documented privacy guard (R7-over-R5) with wide test/comment blast radius; no new egress path.

**KTD3 — The consent section becomes a transparency panel; all four rows are non-interactive.**
Summaries, answers, and day-split become informational rows (title + caption, no control) describing that these run on your connected model and send text only. Frames keeps its "Always off" chip — the one guarantee. The nudge (`consentNudgeVisible` / `consentNudgeCopy`) is removed. *Rationale:* matches "users don't want to toggle jobs"; the section still earns its place as honest data-flow transparency.

**KTD4 — Keep the `setConsent` CLI seam and the Swift decode; only remove the UI toggles.**
`IntelligenceController.setConsent`, the `settings intelligence <row> set` CLI verb, and the settings payload fields remain (now power-user overrides). *Rationale:* removing the toggle UI satisfies the product ask; ripping out the whole consent config surface is wider churn with no user-facing benefit and would delete the override KTD1 relies on.

---

## High-Level Technical Design

`ConsentPolicy.resolve()` outcomes — **before vs. after** (only the shaded rows change; logic is identical, only the default consent flips):

| Task | on-device available? | cloud provider? | Before | After (this plan) |
|---|---|---|---|---|
| Summary / Answers | yes | — | ON_DEVICE | ON_DEVICE (unchanged) |
| Summary / Answers | no | yes, toggle **off (default)** | **NONE — lost** | **CLOUD** |
| Summary / Answers | no | yes, toggle on | CLOUD | CLOUD (unchanged) |
| Summary / Answers | no | none | NONE | NONE (unchanged) |
| Day-split / label | yes | — | ON_DEVICE | ON_DEVICE (unchanged) |
| Day-split / label | no | yes | **HEURISTIC — mechanical names** | **CLOUD** (via summary fallback) |
| Day-split / label | no | none | HEURISTIC | HEURISTIC (unchanged) |
| Frames / images | any | any | NEVER | NEVER (unchanged) |

The "After" column for the two bold rows is produced entirely by KTD1's default flip — no branch in `resolve()` or `terminal_stage` is added. The frames row and the on-device-preferred rows are provably unchanged.

Execution path already in place (no structural change):

```
terminal_stage._finalize_local_tasks
  └─ build_day_split_provider().segment(summary)         # on-device chain / local-server / Unavailable
      ├─ tasks dict          → USE_PROVIDER               # on-device split+label
      ├─ None                → NONE (fail-open)
      └─ PROVIDER_UNAVAILABLE→ resolve_day_split → HEURISTIC
            └─ HEURISTIC branch:
                 1. _summary_cloud_fallback(summary)      # ← now runs whenever cloud configured (KTD1)
                 │     └─ ConsentPolicy.resolve(SUMMARY, on_device_available=False)==CLOUD
                 │           → cloud.segment(stripped summary) → {tasks:[…]} (boundaries+labels)
                 2. _heuristic_local_tasks(...)           # only if cloud declines / no cloud
```

---

## Implementation Units

### U1. Flip the cloud-consent defaults so a connected provider is the consent

**Goal:** Make summaries/answers/day-labeling use a configured cloud provider by default (R1, R2 core), preserving on-device preference and the env/toml override.

**Requirements:** R1, R2, R4.

**Dependencies:** none.

**Files:**
- `src/screencap/config.py` — `get_summary_cloud_consent()` and `get_recall_cloud_consent()` default `False → True`. Update the docstrings (env > toml > default) to state the new default and the "connecting a provider is the consent" rationale.
- `src/screencap/segmentation/consent.py` — docstring/attribute-doc truth-up only (the "Resolved product decision" block and the `summary_cloud_consent`/`recall_cloud_consent` field docs). **No change to `resolve()` logic** — it already gates CLOUD on `cloud_provider is not None`.
- `tests/segmentation/test_consent.py` — update the getter-default tests and the `from_config` default test (below); keep the explicit-`False` override tests as the power-user-override cases.

**Approach:** Change only the two getter defaults. Leave the `ConsentPolicy` dataclass field defaults at `False` (they are the "explicit, unset" value object; the product default is applied in `from_config` via the getters) and note this in the field docstring so the split is intentional, not an oversight. Verify `_CLOUD_CONSENT_ROWS` and the CLI status labels still read correctly with the new default.

**Execution note:** Characterization-first — update the two default-asserting tests to red against the new expected value, then flip the getters.

**Patterns to follow:** the existing getter shape in `config.py` (`get_recall_cloud_consent`), env-key precedence via the shared config resolver.

**Test scenarios** (`tests/segmentation/test_consent.py`, privacy-marked):
- `get_summary_cloud_consent()` with clean env + empty config → **True** (was False).
- `get_recall_cloud_consent()` with clean env + empty config → **True** (was False).
- Env override still wins: `SCREENCAP_SUMMARY_CLOUD_CONSENT=0` → False; `=1` → True.
- TOML `summary_cloud_consent=false` with clean env → False (override retained).
- `ConsentPolicy.from_config()` with empty config → `summary_cloud_consent is True and recall_cloud_consent is True`; and `resolve(SUMMARY, on_device_available=False)` with a configured `cloud_provider` → CLOUD.
- Covers R4: `resolve(FRAMES, …)` → NEVER for every config (unchanged); `resolve(DAY_SPLIT, on_device_available=False)` → HEURISTIC for every config (unchanged, never CLOUD).
- Regression: explicit `ConsentPolicy(cloud_provider="gemini", summary_cloud_consent=False)` still → NONE when on-device unavailable (override path intact).

### U2. Prove and truth-up cloud day-split when no on-device model

**Goal:** Establish, with tests, that under U1 a recording with no on-device model but a configured cloud provider is split **and** labeled by the cloud model — the mechanical heuristic runs only when there's no cloud (R2), and no frames ever egress (R4).

**Requirements:** R2, R4.

**Dependencies:** U1.

**Files:**
- `src/screencap/terminal_stage.py` — docstring truth-up only in `_finalize_local_tasks` / `_summary_cloud_fallback` where they say "if `summary_cloud_consent` is on and a cloud provider is configured" → "when a cloud provider is configured (consent on by default)". No control-flow change (the cloud-before-heuristic ordering already holds).
- `tests/test_terminal_stage_degradation.py` (and/or `tests/test_terminal_stage_segmentation.py`) — add the integration scenarios below.

**Approach:** Drive `_finalize_local_tasks` (or the seam it calls) with a fake provider forcing `PROVIDER_UNAVAILABLE` (on-device unavailable) and a stubbed cloud `get_provider` returning a segmenting provider. Assert cloud tasks are persisted and `_heuristic_local_tasks` is not consulted. Confirm the cloud provider receives only the `stripped=True`-marked text summary (never frames).

**Execution note:** Integration-level — mocks alone won't prove the fallback ordering; exercise the real `_finalize_local_tasks` degradation ladder with a forced-unavailable on-device provider.

**Patterns to follow:** the existing summary-cloud-fallback tests in the terminal-stage test module; `recall`'s consented-cloud fallback test shape.

**Test scenarios:**
- No on-device (PROVIDER_UNAVAILABLE) + cloud configured (default consent) → persisted tasks come from the **cloud** segmentation; the idle-gap heuristic is **not** invoked.
- No on-device + **no** cloud provider → mechanical idle-gap heuristic tasks (unchanged; day-split not fully lost).
- No on-device + cloud configured but cloud `segment()` returns `None`/unavailable → falls through to the heuristic (fail-open ordering preserved).
- The summary handed to the cloud provider carries the builder-set `stripped=True` marker; a fake cloud provider that inspects its input sees text only (R4). 
- Fail-open: cloud `get_provider` raises / unknown name → heuristic, never a raise, never an upload.

### U3. Reframe the Intelligence pane consent section as a transparency panel

**Goal:** Remove the two consent toggles and the nudge; make all four rows non-interactive; retitle the day-split row so it no longer claims "never a cloud task"; keep the frames "Always off" guarantee (R3, R5). Point 1 & 2 UI.

**Requirements:** R1, R2, R3, R5, R6.

**Dependencies:** U1 (copy must describe the shipped behavior).

**Files:**
- `macos/Screencap/Views/Settings/IntelligenceSettingsView.swift` — in `cloudTasksSection`, replace the two `consentToggleRow(...)` calls with informational rows (title + caption, no control); remove the `consentNudgeVisible` block; reframe the day-split `fixedRow` (drop the "On-device" chip or replace with model-agnostic framing); keep the frames `fixedRow` and the trust footer. Remove now-unused `toggleConsent`, `pendingConsentRows`, and the `consentToggleRow` helper if nothing else references them (keep `writeError` if still used by provider writes).
- `macos/Screencap/Views/Settings/IntelligenceSelectionModel.swift` — update `summaryConsentRowCaption`, `recallConsentRowCaption`, `daySplitRowTitle`/`daySplitRowCaption`, and `daySplitChipLabel` (drop or repurpose); remove `consentNudgeCopy` and the `consentNudgeVisible(...)` predicate; update the `allAuditedCopy` corpus to add/remove exactly the strings changed.
- `macos/ScreencapTests/IntelligenceSelectionModelTests.swift` — remove the nudge tests; keep/extend the honest-copy audit test against the revised corpus.
- `macos/ScreencapTests/IntelligenceSettingsTests.swift` — the controller-level `setConsent` / decode / settable-rows tests stay valid (KTD4); adjust only if a test asserted the removed toggle rows render.

**Approach:** The section keeps its header and trust footer. Rows 1–3 (summaries, answers, day-split) render as `fixedRow`-style informational rows without a chip; row 4 (frames) keeps the "Always off" chip. New day-split caption states it runs on your connected model — on-device when available, your cloud model when there isn't one — text only, never screen images. Keep it honest and short.

**Execution note:** UI copy + structure only; no controller/network change. There is no SwiftUI UI-test tooling in this repo — coverage is via the pure-model copy/audit tests and the controller tests.

**Patterns to follow:** the existing `fixedRow` renderer and the `allAuditedCopy` KTD7 discipline (every user-facing string enumerated for the honest-copy audit).

**Test scenarios** (`IntelligenceSelectionModelTests.swift`):
- The honest-copy audit passes over the revised `allAuditedCopy` (no forbidden strings; the day-split caption no longer contains "never a cloud task").
- Every rendered copy string in the reframed section is present in `allAuditedCopy` (no string escapes the audit).
- The removed nudge predicate is gone (test deleted, not left asserting old behavior).
- Test expectation: the view itself has no unit test (no UI test tooling); the model tests above are the coverage.

---

## Scope Boundaries

**In scope:** the config-default flip (U1), day-split cloud coverage + docstring truth-up (U2), and the Intelligence-pane reframe (U3). Behavior stays fallback-only (on-device preferred; cloud only when on-device is unavailable and a provider is configured).

### Deferred to Follow-Up Work
- **Point 3 — "Screen frames or images" toggle** → **SCR-272** (brainstorm; High/Todo). Frames stay `NEVER`.
- **Full removal of the consent config surface** (getters, `settings intelligence` verb, Swift decode, `IntelligenceController.setConsent`). Retained as a power-user override per KTD4; ripping it out is a separate cleanup with no user-facing benefit.
- **`day_split_cloud_consent` / `frames_cloud_consent` settings fields** — decoded but not consulted by the reframed UI; left untouched (frames is SCR-272 territory).

### Out of scope (unchanged guarantees)
- Frames never sent to any cloud model (R4).
- The masked/blocked-app text strip before any provider (R4).
- On-device remaining the preferred target whenever available.

---

## Risks & Dependencies

- **Privacy-lane test churn (highest-value area).** `tests/segmentation/test_consent.py` gates the CI privacy lane. Flipping the getter defaults inverts two default-asserting tests; they must be updated deliberately (not deleted) and the fixed-guard tests (FRAMES→NEVER, DAY_SPLIT→never-CLOUD) must stay green to prove R4 held. Run `pytest -m privacy` locally with `PYTHONPATH=src`.
- **Default-on is a privacy-posture change — but bounded and authorized.** Cloud is still only ever reachable when the user has deliberately connected a provider, and only as the on-device-unavailable fallback; nothing leaves on the default on-device path. This is the explicitly-requested "connecting a provider is the consent" posture. Document it in the config docstrings so a future reader doesn't read it as an accidental weakening.
- **Copy honesty.** The day-split row must stop claiming "never a cloud task" (it can now use cloud). The frames row must keep "Always off." The honest-copy audit (KTD7) is the guardrail — every changed string must be reconciled in `allAuditedCopy`.
- **Semantic subtlety (KTD2).** Internally `DAY_SPLIT` still "never resolves to CLOUD"; the user-visible cloud day-split rides the SUMMARY fallback path. Keep the terminal-stage docstrings accurate so this doesn't read as a contradiction.

---

## Verification Contract

- `pytest -m privacy` (with `PYTHONPATH=src`) green — consent defaults, fixed guards, and terminal-stage degradation scenarios (U1, U2).
- Targeted: `pytest tests/segmentation/test_consent.py tests/test_terminal_stage_degradation.py tests/test_terminal_stage_segmentation.py`.
- macOS: `IntelligenceSelectionModelTests` (honest-copy audit + no-nudge) and `IntelligenceSettingsTests` (controller seam intact) pass under the repo's XcodeGen test flow.
- Manual/behavioral: on a machine with no on-device model + a configured cloud provider, a finalized local recording is split into cloud-labeled tasks (not `task_1`, `task_2`); with no cloud provider, it still splits via the heuristic.

## Definition of Done

- U1–U3 landed; on-device stays the preferred target; frames still `NEVER`; masked/blocked text strip intact.
- The Intelligence pane shows no per-task cloud toggles and no nudge; the day-split row copy is cloud-honest; the frames row keeps its guarantee.
- Privacy lane and macOS model/controller tests green.
- SCR-272 referenced from the plan as the point-3 disposition (no frames behavior changed here).

## Assumptions (pipeline-mode resolutions)

- **Design choice (KTD1): flip config-getter defaults rather than remove the consent gate.** Chosen for minimal blast radius on a privacy-critical module and to retain the CLI override. Alternative (fully removing the `summary_cloud_consent`/`recall_cloud_consent` concept and gating `resolve()` purely on `cloud_provider`) is cleaner semantically but wider (config + CLI + Swift decode + more tests) and removes the override — not chosen for this pass.
- **Day-split via the SUMMARY fallback path (KTD2)** rather than inverting the `DAY_SPLIT`-never-cloud guard — chosen to avoid a wide-blast-radius change to a documented privacy invariant.
- **Consent section kept (as transparency), not deleted.** Assumed the user wants honest data-flow transparency, just not interactive toggles.

## Sources & Research

- `src/screencap/segmentation/consent.py` — `ConsentPolicy.resolve` matrix (fixed guards + fallback preference).
- `src/screencap/segmentation/degrade.py` — `resolve_day_split` never-CLOUD guard.
- `src/screencap/segmentation/routing.py` — `build_day_split_provider` / `build_answer_provider` (on-device-class only).
- `src/screencap/terminal_stage.py` — `_finalize_local_tasks`, `_summary_cloud_fallback`, `_heuristic_local_tasks` (the degradation ladder).
- `src/screencap/segmentation/recall.py` — the sanctioned consented-cloud dispatch pattern mirrored by the summary fallback.
- `src/screencap/config.py` — `get_summary_cloud_consent` / `get_recall_cloud_consent` (default flip site).
- `macos/Screencap/Views/Settings/IntelligenceSettingsView.swift` + `IntelligenceSelectionModel.swift` — the pane and its copy/audit corpus.
- `tests/segmentation/test_consent.py` — the privacy-lane consent matrix tests.
