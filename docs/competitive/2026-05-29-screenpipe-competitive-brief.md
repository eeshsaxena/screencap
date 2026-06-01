# Competitive Brief: ScreenCap vs. screenpipe

**Date:** 2026-05-29 · **Author:** Product · **Decision this informs:** ScreenCap product strategy & prioritization
**Scope:** Full product comparison (features, positioning, GTM, strategic implications)

> **Shelf-life note.** screenpipe ships multiple releases per day and the team is actively
> expanding into a separate enterprise product (Terminator / Mediar). Pricing and positioning
> facts below are accurate as of late May 2026 and will drift fast. Re-verify the *Threats* and
> *Monitoring* sections quarterly. Code claims are grounded in the local clones at
> `../screenpipe` and this repo; market claims are cited inline.

---

## TL;DR

ScreenCap and screenpipe look like the same product — both are local-first macOS recorders that
capture screen + audio with privacy/redaction. **They are not. They serve different jobs:**

- **screenpipe** is a **personal AI memory layer** — "give AI the ability to live your experience."
  Record everything, 24/7, then search it and run AI agents ("pipes") over your own history.
  Consumer + developer ecosystem play. Rewind/Recall/Granola/Otter alternative.
- **ScreenCap** is a **training-data capture pipeline** — produce high-quality, structured,
  *consented* demonstration traces (JSONL interaction events) for training automation/agent models.
  The output is a **corpus**, not a personal memory. B2B / data-flywheel play.

The overlap is in the *plumbing* (capture, storage, redaction), not the *job*. That distinction is
the spine of every recommendation in this brief: **do not chase screenpipe on recall, search, and
breadth — they are years and 4x the code ahead there. Win on the two things that actually serve a
training corpus and that screenpipe structurally under-invests in: consent-grade privacy and
structured ML-ready data export.**

---

## 1. Competitor Overview — screenpipe

| | |
|---|---|
| **Company** | Mediar, Inc. (San Francisco, founded 2024). Founders: Louis Beaumont (CEO, `louis030195`), Matt Diakonov. |
| **Funding / status** | YC **S26**; **$2.8M** raised (announced Oct 30, 2025). ~19k GitHub stars, 360+ releases, 100k+ downloads claimed. |
| **License** | **MIT** core (`Copyright louis030195`) + a proprietary **Enterprise Edition** in `ee/` (Screenpipe Enterprise License, Mediar Inc.). Open-core. |
| **Pricing** | Desktop app ~**$400 one-time** (lifetime); Pro subscription ~$35–39/mo (cloud transcription + encrypted sync); Teams custom. MIT core means the CLI is free and recompilable — paying is effectively voluntary for the app. |
| **Tech** | **Rust monorepo** (~214k LOC, 19 crates) + **Tauri 2 / Next.js** desktop app (342 TS/TSX files). Single `screenpipe-engine` binary hosts capture, axum REST API, MCP server, and the pipes runtime. v0.3.350 (pre-1.0). |
| **Platforms** | **macOS + Windows + Linux** (Linux = build-from-source). |
| **Positioning** | "Leading open source alternative to Rewind.ai (now Limitless), Microsoft Recall, Granola, Otter.ai." Capitalizes on Rewind's Dec-2025 shutdown-after-Meta-acquisition: *"open source… cannot be acquired or shut down."* |

**Strategic context — the expansion bet.** The same team runs **Terminator** (Windows
computer-use/automation SDK, "Playwright for Windows," ~1.5k stars) and **Mediar** (mediar.ai —
enterprise "AI-native RPA," "live in 1 week"). The $2.8M raise was framed as *"give AI hands to
every desktop"* — i.e., the commercial energy is behind **automation**, with screenpipe serving as
the **memory/context input layer**. mediar.ai doesn't mention screenpipe at all. This is an
*expansion*, not a clean pivot — screenpipe is still actively developed — but the revenue thesis
clearly lives in enterprise desktop automation, not the consumer memory app.

*Sources: [GitHub](https://github.com/screenpipe/screenpipe), [funding alert](https://headsup.bot/alert/c1633c8a-c532-47e0-bbd2-812b32a16169), [mediar.ai](https://www.mediar.ai/), [Terminator](https://github.com/mediar-ai/terminator), [HN business-model thread](https://news.ycombinator.com/item?id=41721641).*

---

## 2. Landscape Map

Two axes that make the strategic difference visible:

```
                    OUTPUT = personal memory / recall
                                  ▲
            Rewind/Limitless ·    │    · Microsoft Recall
                                  │      · screenpipe
       consumer / ───────────────┼─────────────── developer /
       individual                │                 data-pipeline
                                  │
                                  │    · ScreenCap
                                  ▼
                    OUTPUT = structured training data / corpus
```

- **Direct competitor for the buyer's wallet:** *weak overlap today.* A user choosing a "rewind my
  day" tool evaluates screenpipe vs. Limitless vs. Recall — not ScreenCap. A team sourcing
  agent-training demonstrations evaluates ScreenCap vs. in-house tooling / data vendors — not
  screenpipe.
- **Direct competitor for the *technology*:** *strong overlap.* Capture engine, redaction, local
  SQLite, audio+Whisper, OCR — these are the same building blocks.
- **Collision risk:** if screenpipe (or Mediar) reframes its corpus of screen+audio history as
  *training data for computer-use agents*, it walks straight into ScreenCap's thesis with a 4x-larger
  codebase and 19k-star distribution. See *Threats*.

---

## 3. Feature Comparison

Rating scale: **Strong** (market-leading) · **Adequate** (functional, undifferentiated) ·
**Weak** (exists, gaps) · **Absent**. Rated from the code in both clones, weighted by what matters
to each product's *own* thesis where noted.

| Capability | ScreenCap | screenpipe | Why it matters |
|---|---|---|---|
| **Capture** | | | |
| Screen capture engine | Strong | Strong | SC: `screencapture` CLI ~150ms + action-gated. SP: ScreenCaptureKit/WGC + event-driven. Both avoid the slow CG path. |
| Structured input/interaction events | **Strong** | Adequate | SC's core asset: `pynput` mouse/keyboard with pressure, modifiers, scroll phase, canonical keys → `action_event` table → JSONL. SP captures a11y/UI events but not as training-formatted traces. |
| Resource efficiency | **Strong** | **Weak** | SC action-gated video keeps footprint low. SP has *documented* 10GB+ RAM / 700% CPU reports ([#183](https://github.com/mediar-ai/screenpipe/issues/183), [#278 memory-leak bounty](https://github.com/mediar-ai/screenpipe/issues/278)). **A real, marketable SC advantage.** |
| Multi-monitor | Adequate | Strong | SP explicitly multi-monitor; SC less emphasized. |
| **Text & content understanding** | | | |
| OCR | Adequate | Strong | SC: Apple Vision, used only as a *privacy* fallback. SP: 5 backends (Apple/Windows/Tesseract/custom/cloud) + accessibility-tree-first (higher quality than pixel OCR). |
| Full-text search | **Absent** | **Strong** | SP: SQLite **FTS5** across OCR/a11y/transcripts via `/search`. SC: no FTS index at all. (Deliberate — see implications.) |
| Semantic / embedding search | Absent | Adequate | SP stores embeddings as BLOBs (brute-force, *not* an ANN index). SC: none. |
| **Audio** | | | |
| Audio capture | Strong | Strong | Both system + mic. SP has macOS 14.4+ per-app audio exclusion. |
| Transcription | Adequate | Strong | SC: Whisper `base` + fallback chain (faster-whisper→openai→API→skip). SP: whisper.cpp `large-v3-turbo` + Silero VAD + Deepgram cloud option. |
| Speaker diarization | Absent | Strong | SP: ONNX 512-dim embeddings + cosine. SC: none. |
| **Privacy & redaction** | | | |
| Capture-time blocking | **Strong** | Weak | SC enforces in real time: foreground-app monitor blocks excluded apps, keystrokes nulled, macOS Secure Input / `AXSecureTextField` detection, fail-closed. SP is mostly *post-hoc* redact + per-app audio exclusion. |
| PII / secret redaction | Strong | Strong | SC: presidio + GLiNER + detect-secrets + app-classification matrix. SP: `screenpipe-redact` 8 adapters incl. **visual-region detection** (rfdetr/MLX). Roughly at parity, different strengths (SC text/policy depth vs. SP visual). |
| At-rest encryption | Adequate | Strong | SC: KEK in Keychain for network-body capture. SP: `vault` AES-256-GCM over DB+screenshots+audio, OS-keychain key. |
| **Consent workflow** | **Strong** | **Absent** | SC: native pre-upload review screen → binary Upload/Cancel before data leaves the machine. SP: record-all; consent is the device owner's, on behalf of everyone in frame/call (a top HN criticism). **The sharpest structural divergence.** |
| **Data output & ML training** | | | |
| Structured event export (JSONL) | **Strong** | Adequate | SC's reason to exist. SP exposes data via DB/API but it's not training-formatted. |
| Training-corpus pipeline | **Strong** | **Absent** | SC: `upload`→GCS signed URLs, `download`, data-flywheel thesis. Not screenpipe's purpose. |
| **Developer ecosystem** | | | |
| Local/REST API | Adequate | Strong | SC: internal `/v0/*` control plane over UNIX socket. SP: full REST + OpenAPI on :3030, websockets, connector surface (gmail/slack/calendar/browser). |
| MCP server | Absent | Strong | SP: `screenpipe-mcp` for Claude/Cursor/VS Code. SC: none. |
| Public SDKs | Absent | Strong | SP: embeddable capture SDK for Electron/Swift/Tauri/Node (under EE license). SC: none. |
| Plugin / agent runtime | Absent | Strong | SP: "pipes" (markdown agents) + **per-pipe cryptographic data permissions** (genuinely good design). SC: LLM scoped to auto-naming + transcription only. |
| **App & platform** | | | |
| Native app shell | Adequate | Strong | SC: SwiftUI app wrapping the CLI (growing, review screen incoming). SP: mature Tauri timeline/DVR app. |
| Windows | Adequate | Strong | SC: separate `screencap-windows` codebase. SP: first-class single codebase. |
| Linux | Absent | Adequate | SP: build-from-source. SC: none. |

**Honest read:** screenpipe is ahead on **breadth, search, audio intelligence, developer surface,
and platform coverage** — and it isn't close. ScreenCap is ahead on **resource efficiency,
capture-time privacy enforcement, consent workflow, and structured training-data export** — exactly
the axes its thesis depends on, and exactly the axes screenpipe's "record everything for me" model
de-prioritizes.

---

## 4. Positioning Analysis

| | ScreenCap | screenpipe |
|---|---|---|
| **Category claim** | "Local-first macOS recorder for building **ML-ready demonstration data** for automation/agent training." | "24/7 local AI **memory layer** — give AI the ability to live your experience." |
| **Target customer** | Teams sourcing reproducible, structured, consented interaction traces (the "internal-tool-heavy operator" persona). | Knowledge workers (recall/meetings) **and** developers (build on the memory layer) **and** enterprises (via Mediar). |
| **Key differentiator** | Structured event export + consent-grade privacy → a defensible, ethically-sourced corpus. | Open-source + breadth + agent/pipe ecosystem; "can't be acquired or shut down." |
| **Value proposition** | Capture → curate → train, with provable consent. | Never forget anything; let AI act on your whole digital life. |
| **Proof points** | Deep privacy subsystem, SECURITY.md threat model, daemon architecture. | 19k stars, YC, $2.8M, 360+ releases, MIT. |

**Positioning gaps & opportunities for ScreenCap:**
- **Unclaimed position ScreenCap can own:** *"consent-grade, structured training data for
  computer-use agents."* No one in the screen-recording space owns "ethically-sourced agent
  training corpus." screenpipe **can't** credibly claim it — their model is record-everything,
  consent-by-device-owner, which is their single biggest public criticism.
- **screenpipe's vulnerable claim:** "100% local / private." It's *mostly* true but has opt-in
  cloud paths (Deepgram STT, Tinfoil privacy-filter, cloud OCR). Combined with the consent-for-others
  problem, "private" is a position they cannot fully defend — and a place ScreenCap's capture-time
  enforcement is genuinely stronger.
- **Crowded position to avoid:** "open-source local Rewind alternative." Saturated (screenpipe,
  Remio, ScreenMemory, Littlebird, Pieces, Omi…). Do not plant a flag here.

---

## 5. Strengths & Weaknesses

**screenpipe — strengths (be honest):**
- Real open-source traction and momentum (19k stars, YC, daily releases) → distribution & trust.
- Genuine engineering depth: a11y-tree-first capture, multi-backend OCR, diarization, FTS5, a
  redaction subsystem with visual-region detection, at-rest encryption, E2E-encrypted sync.
- Best-in-class developer surface: REST + OpenAPI + MCP + multi-language SDKs + a plugin runtime
  with thoughtfully-designed per-pipe cryptographic permissions.
- Cross-platform (mac/Win/Linux) from one codebase.

**screenpipe — weaknesses (evidence-based, not dismissive):**
- **Resource hog:** documented 10GB+ RAM and runaway CPU; a paid bounty to fix a memory leak.
- **Pricing incoherence:** MIT license undermines the $400 app; FOMO price timers drew HN criticism.
- **Consent/privacy exposure:** records others without their consent; the dominant community critique.
- **Quality complaints:** transcription accuracy, OCR misses, "confidently wrong" automation.
- **Strategic ambiguity:** attention split between screenpipe (consumer memory) and Terminator/Mediar
  (enterprise RPA); unclear which is the real product.
- **Onboarding friction:** CLI-vs-app, self-compile-vs-pay, pipe customization.

**ScreenCap — strengths:**
- Sharp, defensible thesis (training corpus) that screenpipe's architecture doesn't serve.
- Deepest-in-class **capture-time** privacy + consent workflow.
- Efficient capture (action-gated) → low footprint, the inverse of screenpipe's biggest pain.
- Clean licensing for B2B (AGPL + commercial dual license).
- Disciplined engineering: daemon architecture, documented threat model, strong planning hygiene.

**ScreenCap — weaknesses:**
- A fraction of the surface area: no FTS/search, no MCP/SDK/plugin ecosystem, no diarization.
- macOS-primary; Windows is a separate codebase (maintenance drag, feature skew).
- Single-/small-team scale vs. a funded, 19k-star project.
- Cloud is a first-party GCS backend, not a general/extensible surface.
- No public traction signals yet (the corpus opt-in metric is still at zero pending the GUI flow).

---

## 6. Opportunities

1. **Own "consent-grade training data."** screenpipe structurally can't. The pre-upload review
   screen is the product embodiment of this — it's not a feature, it's the moat. Lead with it.
2. **Turn screenpipe's resource problem into a wedge.** Publish honest footprint numbers
   (CPU/RAM/disk per hour) for action-gated capture vs. always-on. This is a concrete, verifiable
   differentiator for any team running capture across many machines.
3. **Exploit the strategic-attention gap.** While Mediar chases enterprise Windows RPA, the
   *macOS, consented, structured-data* niche is comparatively unattended.
4. **Position against the Rewind shutdown narrative differently.** screenpipe says "we can't be
   shut down." ScreenCap can say "your contributors *chose* to contribute, and can prove it" —
   provenance + consent, which matters more for a *training corpus* than for personal recall.
5. **Borrow screenpipe's best idea selectively:** per-pipe cryptographic data permissions is a
   strong model for *who/what can touch captured data*. Worth studying for ScreenCap's
   corpus-access/governance story (not for building a plugin ecosystem).

---

## 7. Threats

1. **The collision scenario (highest-severity).** screenpipe/Mediar reframes its screen+audio
   history as *"training data for computer-use agents"* — directly into ScreenCap's thesis, with 4x
   the code, MCP/SDK distribution, cross-platform reach, and funding. Their Terminator work makes
   this adjacency real, not hypothetical. **This is the nightmare move.**
2. **Commoditized capture + redaction.** screenpipe's MIT capture/redaction crates could become the
   default building blocks others adopt, eroding ScreenCap's engineering lead in the plumbing.
3. **Developer mindshare.** MCP server + SDKs mean screenpipe is where developers integrate screen
   context today. If "screen data for AI" *means* screenpipe to developers, ScreenCap fights uphill.
4. **Funding & velocity asymmetry.** $2.8M + YC + daily releases vs. a small team. They can close
   feature gaps (incl. a consent flow) faster than ScreenCap can close breadth gaps.

**Where ScreenCap is most vulnerable:** search/recall (absent), developer ecosystem (absent), and
cross-platform parity. *Mitigant:* none of these are core to the training-corpus thesis — which is
exactly why the strategy below says don't defend them at parity.

---

## 8. Strategic Implications (the payload)

**Frame:** Compete on *thesis*, not *feature count*. Every dollar spent reaching parity on
screenpipe's turf (recall search, pipes, SDKs, Linux) is a dollar not spent widening the consent +
structured-data moat that screenpipe can't follow you into.

**Build / accelerate:**
- **The pre-upload review & consent flow** (`docs/plans/2026-05-27-002-feat-upload-review-screen-plan.md`)
  is the single highest-leverage item. It's the corpus's ethical and legal foundation *and* the
  positioning wedge. Ship it, then make consent + provenance a first-class, auditable, *marketed*
  capability — not an internal detail.
- **Structured-export quality & schema stability.** The JSONL event format is the product. Treat it
  like an API: version it, document it, make it the best agent-training trace format available.
- **Footprint benchmarking.** Instrument and publish capture cost. Cheap to do, directly counters
  screenpipe's most-complained-about flaw.

**Differentiate (don't match):**
- **Privacy = capture-time enforcement + consent**, vs. screenpipe's post-hoc + record-all. This is
  already ScreenCap's strongest asset; widen the gap (secure-input coverage, app-matrix breadth,
  audit/provenance for the corpus).
- **Curation, not recall.** If search gets built, build *dataset-curation* query/filtering (find,
  filter, and select traces for a training set) — not "search my life." Different job, serves the
  thesis, avoids a losing race against FTS5 + embeddings.

**Achieve parity only where the thesis demands it:**
- **Transcription quality** matters if transcripts feed training data — adopt a larger Whisper model
  / VAD; cheap, high-ROI, closes a real gap.
- Diarization, multi-backend OCR, full a11y-tree extraction: parity *only* if a customer's training
  use-case requires them. Otherwise defer.

**Deprioritize / explicitly decline:**
- General recall/full-text search as a headline feature. (Document the decision — it will keep coming
  up because screenpipe makes it look table-stakes. It isn't, for this thesis.)
- A plugin/agent ecosystem and public SDK marketplace. screenpipe is far ahead and it's a different
  business; ScreenCap's "agents" surface stays scoped (naming/transcription).
- Linux. No thesis justification today.

**Positioning / messaging adjustments:**
- Plant the flag on **"consent-grade, structured training data for computer-use agents."**
- Never enter the "open-source local Rewind alternative" category — it's crowded and off-thesis.
- When compared to screenpipe, redirect the axis: *memory for you* vs. *training data for models,
  with provable consent*. Don't argue feature-for-feature; argue job-to-be-done.

---

## 9. What to Monitor

| Signal | Why | Cadence |
|---|---|---|
| Mediar/Terminator messaging shift toward "training data" / "demonstrations" / "agent traces" | The collision trigger | Monthly |
| screenpipe adding a **consent/review-before-upload** flow | Directly attacks ScreenCap's wedge | Monthly |
| screenpipe export formats aimed at *model training* (vs. search/recall) | Thesis encroachment | Monthly |
| Resource-usage fixes landing (RAM/CPU) | Erodes the efficiency wedge | Quarterly |
| screenpipe pricing/license changes (esp. moving core off MIT) | Signals monetization seriousness | Quarterly |
| macOS consent SDK / capture SDK adoption by other tools | Commoditization of the plumbing | Quarterly |
| Hiring signals at Mediar (ML data / dataset engineers) | Strongest leading indicator of a pivot into ScreenCap's space | Quarterly |

---

*Caveat on fairness:* screenpipe is a genuinely strong, well-engineered project with real traction;
this brief intentionally credits its lead on breadth/search/ecosystem. The strategic case for
ScreenCap is **not** "we're better" — it's "we're solving a different job that their architecture and
consent model can't serve, and we should refuse to be dragged onto their turf."
