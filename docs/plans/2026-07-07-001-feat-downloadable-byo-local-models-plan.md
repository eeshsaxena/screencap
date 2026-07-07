---
title: Downloadable & Bring-Your-Own Local Models - Plan
type: feat
date: 2026-07-07
topic: downloadable-byo-local-models
artifact_contract: ce-unified-plan/v1
artifact_readiness: requirements-only
product_contract_source: ce-brainstorm
execution: code
---

# Downloadable & Bring-Your-Own Local Models - Plan

## Goal Capsule

- **Objective:** Extend the shipped Intelligence provider with two opt-in local backends — a downloadable small model (MLX + llama.cpp) and a bring-your-own local model — so any Mac gets named tasks locally without waiting for macOS 26 adoption.
- **Product authority:** Rute (product owner).
- **Open blockers:** None blocking planning. The runtime split (llama.cpp alone vs MLX + llama.cpp) is a planning-time optimization, not a product blocker.

---

## Product Contract

### Summary

Two opt-in backends behind the shipped Intelligence adapter let any Mac produce named tasks on-device without macOS 26: a small model the user **downloads** (MLX on Apple Silicon, llama.cpp on Intel) and a **bring-your-own** local model (Ollama / LM Studio / OpenAI-compatible). Both inherit the existing privacy strip and per-task consent matrix. The download is offered at onboarding, lives in the Intelligence pane, and carries a standing sidebar hint; output is confidence-gated so a low-confidence result stays unnamed rather than misleading.

### Problem Frame

PR #345 made segmentation on-device by default, but the default backend — Apple Foundation Models — needs macOS 26 + Apple Intelligence. Every Mac below that (macOS 13–25, and all Intel Macs) falls back to the **unnamed idle-gap heuristic** for day-splitting. So the headline "named tasks, locally" experience is off for the majority of users until macOS 26 adoption rises over the next year or two. This closes that gap for anyone willing to opt in.

### Key Decisions

- **Additive backends, not a rebuild.** Both new backends slot behind the existing provider adapter and inherit the privacy strip, per-task consent matrix, and degradation ladder unchanged. Nothing in #345 is modified.
- **Cover every Mac that opts in.** The downloadable model runs via MLX on Apple Silicon and llama.cpp on Intel, so no supported Mac is stuck on the unnamed heuristic once the user opts in.
- **Opt-in download, never bundled.** The model downloads on user consent (size disclosed, integrity-verified), not shipped in the app — keeping the app light was the reason a bundled default was rejected.
- **Layered, low-pressure discovery.** Offered at onboarding, present as a row in the Intelligence pane, and a standing dismissible sidebar hint that enabling it enhances the experience. No per-recording interrupt.
- **Beat the heuristic, never mislead.** The bar is names users prefer over the unnamed heuristic — not Gemini parity. A low-confidence or schema-invalid result falls back to unnamed rather than showing a wrong name.
- **"Bring your own" is a local server.** A local endpoint (Ollama/LM Studio on the Mac) is treated as on-device (day-splitting allowed, nothing leaves); a remote OpenAI-compatible endpoint is treated as a cloud provider (per-task consent-gated, day-split never — R7 of the shipped matrix).

### Requirements

**Downloadable local model**

- R1. A user can opt in to download a small local model that produces named tasks on-device; the app never downloads it without consent.
- R2. The downloaded model runs locally on both Apple Silicon (MLX) and Intel (llama.cpp), so any supported Mac that opts in gets named tasks.
- R3. The download discloses its size before downloading and is integrity-verified (checksum/signature) before first use.

**Bring-your-own model**

- R4. A user can point the Intelligence provider at a local model server they run (Ollama / LM Studio / OpenAI-compatible endpoint).
- R5. A BYO local endpoint is treated as on-device (day-splitting allowed, nothing leaves the Mac); a remote endpoint is treated as a cloud provider (per-task consent-gated, day-split never).

**Discovery & offer**

- R6. The download is offered at onboarding, available as a row in the Intelligence settings pane, and surfaced as a standing, dismissible sidebar hint that enabling it enhances the experience.
- R7. The offer is opt-in and never blocks a recording; declining leaves the user on the existing heuristic / BYO / cloud paths and does not re-prompt intrusively.

**Quality & routing**

- R8. Both backends slot into the existing degradation ladder: Apple Foundation Models (macOS 26) → downloaded local model (if opted in) → idle-gap heuristic; a BYO-local endpoint is an on-device option, BYO-remote/cloud is consent-gated.
- R9. Output is confidence-gated: a low-confidence or schema-invalid result yields unnamed task boundaries (or the heuristic), never a wrong or hallucinated name.
- R10. The new backends inherit the shipped privacy strip and per-task consent matrix unchanged — masked/blocked content is stripped before any local model, and day-splitting never leaves the Mac.

### Key Flows

- F1. Opt into the downloaded model
  - **Trigger:** The user accepts the offer (onboarding, settings row, or sidebar hint).
  - **Steps:** Consent + size shown → download → integrity-verify → subsequent local recordings get named tasks on-device (MLX or llama.cpp per platform).
  - **Covers:** R1, R2, R3, R6
- F2. Bring your own local model
  - **Trigger:** The user configures a model endpoint.
  - **Steps:** A local endpoint is treated as on-device (day-split runs locally, nothing leaves); a remote endpoint is treated as a cloud provider (consent-gated, day-split refused).
  - **Covers:** R4, R5
- F3. Low-confidence result
  - **Trigger:** A local model returns a low-confidence or invalid result for a task.
  - **Steps:** Keep the task boundary but leave it unnamed; never surface a wrong name.
  - **Covers:** R9

### Acceptance Examples

- AE1. **Covers R2.** Given an Intel Mac with the downloaded model, When a local recording is segmented, Then it produces named tasks on-device (via llama.cpp) with nothing uploaded.
- AE2. **Covers R5.** Given a BYO local Ollama endpoint, When the day is split, Then it runs against the local endpoint and nothing leaves the Mac; Given a BYO *remote* endpoint, When the day would split, Then day-splitting does not use it (consent-gated).
- AE3. **Covers R9.** Given the downloaded model returns a low-confidence/invalid result for a task, When tasks are shown, Then that task is unnamed rather than showing a wrong name.
- AE4. **Covers R1, R7.** Given a user declines the download, When they record, Then they get the existing heuristic (or their configured BYO/cloud) and are not re-prompted intrusively.

### Success Criteria

- On a representative eval, the downloaded model's task names are preferred over the unnamed heuristic and are not misleading — no wrong names surface (the confidence gate holds).
- Any opted-in Mac, Apple Silicon or Intel, produces named tasks locally with nothing leaving the Mac.
- Adding a BYO model or the download is config/UX only — no change to the shipped #345 provider core.

### Scope Boundaries

**Deferred for later**

- Matching Gemini/cloud naming quality — the bar here is "beat the heuristic, never mislead."

**Outside this product's identity**

- Bundling a model in the app by default (opt-in download only).
- Any modification to the shipped #345 provider core — these backends are purely additive.
- Non-macOS platforms.

### Dependencies / Assumptions

- New local-inference dependencies: MLX (`mlx-lm`, Apple Silicon) and a llama.cpp binding (Intel + universal fallback). Assumption: a ~3B quantized model clears the "beat the heuristic" bar with the existing validator plus the confidence gate; the eval confirms this before named output ships.
- The shipped provider adapter, privacy strip, consent matrix, degradation ladder, settings pane, and CLI (PR #345 / `docs/plans/2026-07-06-002-feat-local-first-intelligence-plan.md`) exist and are the extension points.
- BYO assumes the user runs a local OpenAI-compatible / Ollama server; local-vs-remote endpoint classification drives the privacy treatment.

### Outstanding Questions

**Deferred to planning**

- Runtime split: llama.cpp alone (one universal runtime — Metal on Apple Silicon, CPU on Intel) vs MLX-for-Apple-Silicon + llama.cpp-for-Intel (best per-platform, weighed against the run-all-day overhead target).
- Model choice, size, and quantization (~3B class); the download source and integrity-verification mechanism; on-disk storage location.
- The confidence-gate mechanism and threshold (per-task vs whole-session), building on the existing validator's repair/reject path.
- The eval design comparing the downloaded model against the heuristic (and optionally Gemini) for the "beat the heuristic, never mislead" bar.
- Exact local-vs-remote endpoint classification for BYO (localhost/LAN vs remote URL).

### Sources / Research

- Shipped Intelligence architecture (the extension points): `docs/plans/2026-07-06-002-feat-local-first-intelligence-plan.md` (PR #345) — provider adapter `src/screencap/segmentation/provider.py`, privacy strip `src/screencap/segmentation/activity_summary.py`, consent matrix `src/screencap/segmentation/consent.py`, degradation ladder `src/screencap/segmentation/degrade.py`, on-device provider `src/screencap/segmentation/providers/ondevice.py`, settings pane `macos/ScreenCap/Views/Settings/IntelligenceSettingsView.swift`, CLI `screencap settings intelligence`.
- The deferred "any local server (OpenAI-compatible)" backend, noted in the #345 plan's Scope Boundaries → Deferred to Follow-Up Work, and the "Add another provider…" row in the settings design.
- `STRATEGY.md` — macOS-only, Apple Silicon + Intel install base, privacy-first, "run all day" low-overhead target.
