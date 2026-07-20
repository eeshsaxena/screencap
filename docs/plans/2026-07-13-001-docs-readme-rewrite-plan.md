---
title: README Rewrite with Codebase Grounding - Plan
type: docs
date: 2026-07-13
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-plan-bootstrap
execution: code
---

# README Rewrite with Codebase Grounding - Plan

## Goal Capsule

- **Objective:** Replace the stale root `README.md` with a version grounded in the codebase as it exists today (CLI v0.24.6, macOS app v0.12.0), positioned per `STRATEGY.md` and the screenpipe competitive brief, written with a natural human voice.
- **Authority hierarchy:** Source code and `--help` output > `CHANGELOG.md` > `STRATEGY.md` / `docs/competitive/2026-05-29-screenpipe-competitive-brief.md` > current `README.md` prose. When any doc contradicts the code, the code wins.
- **Stop conditions:** Do not change any source code, `SECURITY.md`, `CHANGELOG.md`, or `macos/README.md`. Do not invent features, pricing, or install channels that cannot be verified in the repo. Do not add competitor names to the README.
- **Execution profile:** Docs-only change; verification is claim-by-claim fact-checking against the codebase, not a test suite.

---

## Product Contract

### Summary

The root `README.md` still describes Screencap as a bare CLI for "building ML-ready demonstration data" — the pre-pivot thesis. Since it was written, the product shipped: a supervised daemon architecture, a native SwiftUI macOS app (its own release track, now v0.12.0, launched publicly 2026-07-07), cloud sign-in with per-user uploads and opt-in E2EE, a review-before-upload consent flow, local search (content index + backfill), an MCP server for agents, and on-device intelligence (segmentation/journal with per-task consent). The README must be rewritten to reflect what the product is now and where it is going, with positioning informed by the direct competitor screenpipe, in a voice that reads like a person wrote it.

### Problem Frame

A README is the repo's front door. Today's version misleads on identity (training-data pipeline vs. private screen memory for operators + agents), omits the flagship macOS app entirely, documents a command surface that has since tripled (no `status`, `serve`, `mcp`, `search`, `backfill`, `login`, `settings`, `storage`, `model`, `clip`, `review-data` …), and says nothing about the differentiators the strategy says to lead with (capture-time privacy enforcement, consent before anything leaves the machine, light footprint). Anyone evaluating the project — a user, a contributor, or a competitor-comparison shopper — gets a two-months-stale picture.

### Requirements

**Accuracy (codebase grounding)**

- R1. Every command, flag, default value, file path, and behavior claim in the new README is verified against the current source (`src/screencap/cli/`, `src/screencap/config.py`, installer/homebrew assets) or live `--help` output. No stale flags, no invented ones.
- R2. The README covers the product surface that now exists and matters to a reader: macOS app + CLI (two release tracks), daemon architecture (`screencap serve`, LaunchAgent, `screencap setup`), search (consent-gated via `screencap search enable`, default off; content index + backfill; querying happens through the macOS app, the daemon API, and MCP — there is no CLI query verb), MCP server (`screencap mcp`, pointing to `docs/mcp-client-setup.md`), cloud sign-in/upload/download with review-before-upload and opt-in E2EE, privacy system (capture-time enforcement + post-hoc scrubbing), and structured JSONL export.
- R3. Sections of the current README that are still accurate (privacy modes, app classification matrix, disk-space protection, stop semantics) are verified and kept/condensed rather than rewritten from scratch.

**Positioning (competitive grounding)**

- R4. Positioning leads with private-by-architecture screen memory for people who work across many tools, searchable by you and your agents; owns "consent-grade privacy" and capture-time (fail-closed) enforcement; keeps structured ML-ready export as a capability, not the headline. This deliberately supersedes the competitive brief's thesis-level guidance (§8: training-corpus-first flag, "curation not recall") — the post-May shipping record (search, MCP, journal, personal cloud, public launch) settled the identity per A2. Only the brief's tactical guidance still binds: never name competitors, own consent-grade privacy and capture-time enforcement, use the footprint wedge, redirect the axis rather than comparing features.
- R5. No competitor is named and no feature-for-feature comparison table appears; differentiation is expressed through the category axis (what always-on recall tools can't credibly claim: nothing leaves the machine without review, blocked content is never written to disk, light footprint). This is the brief's explicit "redirect the axis" guidance.
- R6. A short "where this is going" section reflects the four `STRATEGY.md` tracks in public-friendly language, with no internal ticket IDs (SCR-*) or unshipped-feature promises stated as fact. The tracks are the frame, not the copy source — `STRATEGY.md` self-flags its crux sections for revision and predates the launch, so render the section's emphasis from the post-launch shipping record.

**Voice and shape**

- R7. Tone is warm, concrete, and honest — short sentences, no hype-slop, no badge wall, macOS-only stated plainly. Reads like a maintainer wrote it, not a launch page.
- R8. The command reference is slimmed: full detail for the daily verbs (start/stop/status, export, setup/settings, search enable/status), one-line summaries + `--help` pointer for the rest. The README should get shorter or stay near current length (~460 lines), not balloon.

### Scope Boundaries

- **In scope:** `README.md` only.
- **Deferred to follow-up work:** website copy (screencap.sh lives in the sibling `screencap-website` repo), `macos/README.md` (developer doc, still accurate), a docs-site restructure, refreshing the competitive brief itself.
- **Out of scope:** any code, license, or security-doc change; adding screenshots/GIFs (worth doing later with real app captures, not something this pass can fabricate).

---

## Planning Contract

### Key Technical Decisions

- **KTD1 — App-first framing, CLI-substantial body.** The hero and quick start lead with the macOS app (the strategy's load-bearing track and the actual public product since 2026-07-07, downloadable at screencap.sh), then give the CLI/daemon full first-class treatment for the repo's developer audience. Rationale: the repo README serves both evaluators and contributors; the old README served only CLI users.
- **KTD2 — Differentiate without naming.** The competitive brief recommends redirecting the axis rather than arguing feature-for-feature; naming screenpipe in the README invites a comparison Screencap should not fight on breadth. Competitor awareness shapes *which claims we lead with* (consent, capture-time blocking, footprint, structured data), not a comparison section. Note the brief is consulted for these tactics only — its thesis-level positioning recommendation is superseded per R4.
- **KTD3 — No prices or unverifiable channels in the README.** Cloud sync is described as an optional signed-in feature with a link to screencap.sh; the $5/mo figure and any pricing live on the website where they can change without a repo PR. Install channels are documented only if verifiable in-repo or on the live installer (curl installer, from-source; Homebrew only if the formula's distribution status can be confirmed).
- **KTD4 — Verified-fact inventory before prose.** Because a README rewrite fails via confidently-stale claims, U1 produces a checked fact sheet (command surface from `--help`, defaults from `config.py`, env vars, directory layout, install commands) that U2 writes from and U3 audits against. `PYTHONPATH=src` is required for any in-worktree CLI invocation (editable install points at whichever checkout last ran `pip install -e`).
- **KTD5 — Old thesis handled by reframing, not deletion.** "ML-ready demonstration data" framing becomes a capability section (structured JSONL export + opt-in corpus) consistent with the strategy's data-flywheel track, so existing users of `screencap export` still find themselves in the doc.

### Assumptions

- A1. The README stays a single file; no split into `docs/` pages this pass.
- A2. The publicly presentable identity is the `STRATEGY.md` one (operator screen memory + agent access), which the post-May shipping record (search, MCP, journal, personal cloud, PH launch) confirms over the older training-corpus-first framing.
- A3. "Check screenpipe reference" means the existing competitive materials (`docs/competitive/2026-05-29-screenpipe-competitive-brief.md`, the local screenpipe clone) — no fresh web competitive research is required for a README pass.
- A4. Contributor/dev sections (testing, from-source install) remain in the README in condensed form.

### Sources

- `STRATEGY.md` — target problem, approach, four tracks, "Not working on Windows".
- `docs/competitive/2026-05-29-screenpipe-competitive-brief.md` — positioning guidance (§4, §8): own consent-grade privacy, redirect the axis, avoid the "open-source Rewind alternative" flag.
- `CHANGELOG.md` 0.22.0–0.24.0 — cloud sign-in, disk-first pipeline, reviewed==uploaded, agent-memory retrieval, segmentation/intelligence, billing.
- `CLAUDE.md` — daemon architecture, privacy package DAG, content index/backfill/MCP details.
- `src/screencap/cli/__init__.py` — actual command surface (start, stop, status, list, view, info, apps, rename, transcribe, export, scrub, upload, download, setup, settings {privacy,intelligence}, login/logout/whoami, checkout-url/portal-url, reconcile-entitlement, serve, mcp, clip, review-data, inspect-data, update, and groups: search {enable,status}, backfill, network, storage, model, e2ee).
- `../screenpipe/README.md` (local clone) — competitor's public framing ("records everything you do, say, hear 24/7"), which R5's axis-redirect positions against.
- Release tags: CLI `v0.24.x` and `macos-app-v0.12.0` (two tracks).

---

## Implementation Units

### U1. Verified fact inventory

- **Goal:** A scratch fact sheet of every claim the new README will make, each checked against source.
- **Requirements:** R1, R2, R3.
- **Dependencies:** none.
- **Files:** read-only pass over `src/screencap/cli/__init__.py` (+ subcommand modules), `src/screencap/config.py`, `pyproject.toml`, `homebrew/screencap.rb`, `SECURITY.md`, `docs/mcp-client-setup.md`, `CHANGELOG.md`. Fact sheet lives in the scratchpad, not the repo.
- **Approach:** Enumerate the live command surface (prefer `PYTHONPATH=src python3 -m screencap --help` and per-command `--help`; fall back to source when running is impractical). Record: command list + one-line purposes; flags/defaults for the daily verbs (R8); env vars and config keys actually read by `config.py`; CLI version from `pyproject.toml` (git tags in a worktree may lag); recording directory layout as written today; install commands (curl installer URL, from-source; confirm or drop Homebrew — `homebrew/screencap.rb` looks like a placeholder, so expect drop); the app's public download source (fetch screencap.sh live or verify against GitHub `macos-app-v*` releases) with a source pointer; requirements (macOS version, permissions incl. Input Monitoring if required); which privacy-section claims in the old README still match `src/screencap/privacy/policy.py` and `enforcement/`.
- **Test scenarios:** Test expectation: none — read-only research unit; its output is checked by U3.
- **Verification:** Fact sheet exists and every entry carries a source pointer (file:line or `--help` capture).

### U2. Rewrite README.md

- **Goal:** The new README, written from the U1 fact sheet with the R4-R7 voice and structure.
- **Requirements:** R2-R8.
- **Dependencies:** U1.
- **Files:** `README.md`.
- **Approach:** Target structure — (1) hero: name + two-sentence identity (private screen memory on your Mac; you and your agents can search it; nothing leaves without your say-so); (2) "why it's different" — 3-5 concrete claims (capture-time fail-closed blocking, review-before-upload, local-only DB rule, action-gated capture — video written around user actions rather than a 24/7 firehose, stated mechanically since no published footprint benchmarks exist yet — and structured events), each one verifiable in source; (3) install — app download (screencap.sh) then CLI (curl installer, from source); (4) quick start — app one-liner, then CLI session (`start` → `stop`/`status` → `view`/`export`, with `screencap search enable` shown as the one-time consent moment that turns on local search); (5) search & agents — content index (default off, consent-gated via `search enable`), backfill, querying via the macOS app / daemon API / MCP (`screencap mcp` + `docs/mcp-client-setup.md`) — never document a CLI query verb, none exists; (6) privacy — condensed modes/actions/classification from the verified old content + link to `SECURITY.md`; (7) cloud (optional) — sign-in, review-before-upload, opt-in E2EE, `recording.db` never uploaded; (8) ML-ready export (KTD5); (9) command reference per R8; (10) configuration (verified keys/env vars); (11) where this is going (R6); (12) contributing/testing; (13) license (dual AGPL/commercial, kept as-is). Write prose per R7; sanity-check total length against R8.
- **Patterns to follow:** current README's factual tables where retained; `STRATEGY.md` tone (plain, honest, no superlatives) over the competitor's hype register.
- **Test scenarios:** Test expectation: none — docs-only unit; correctness is enforced by U3's audit.
- **Verification:** README renders cleanly (heading hierarchy, tables, fenced blocks), every claim traces to the U1 fact sheet, and R5 holds (no competitor names anywhere in the file).

### U3. Adversarial claim audit

- **Goal:** Independent re-check of the written README against the codebase, catching anything U2 carried over on faith.
- **Requirements:** R1, R3, R5, R6.
- **Dependencies:** U2.
- **Files:** `README.md` (fixes only).
- **Approach:** Walk the finished README claim by claim as a skeptic: re-run/re-read the source for each command, flag, default, path, permission, and behavior statement; verify every relative link resolves in-repo (`SECURITY.md`, `LICENSE`, `docs/mcp-client-setup.md`) and that external URLs (screencap.sh, installer URL) resolve or trace to the U1 fact sheet's source pointers; grep the file for internal ticket IDs (`SCR-`), competitor names, user-home absolute paths (`/Users/…`), and pricing figures — all must be absent; confirm the roadmap section promises nothing unshipped as already-done.
- **Test scenarios:** Test expectation: none — the audit itself is the test; findings are fixed in place.
- **Verification:** Zero unresolved discrepancies between README statements and source; link check passes; forbidden-token grep is clean.

---

## Verification Contract

| Gate | Command / check | Applies to |
|---|---|---|
| Command-surface truth | `PYTHONPATH=src python3 -m screencap --help` (+ per-command `--help`) diffed against README | U1, U3 |
| Link integrity | every repo-relative link in `README.md` resolves to an existing file; external URLs resolve live or trace to a U1 fact-sheet source pointer | U3 |
| Forbidden tokens | `grep -inE 'SCR-[0-9]+|screenpipe|rewind|dayflow|\$[0-9]|/Users/' README.md` returns nothing (case-insensitive; covers competitor names incl. Dayflow from STRATEGY.md, pricing figures, user-home absolute paths) | U3 |
| Render sanity | README preview renders with correct heading/table/code-block structure | U2 |
| No code touched | `git diff --stat` shows only `README.md` | all |

No pytest lane applies — this is a docs-only change with no runtime surface.

## Definition of Done

- New `README.md` committed, covering R1-R8, with the U3 audit finding zero discrepancies.
- Only `README.md` changed in the diff.
- No competitor names, ticket IDs, absolute paths, or pricing figures in the file.
- Old-README content that was still accurate is preserved (condensed), not lost.
