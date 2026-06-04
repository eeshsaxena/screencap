---
date: 2026-06-03
topic: next-work-strategy
focus: what to work on next based on the strategy
mode: repo-grounded
---

# Ideation: What to Work On Next, Weighted by the Strategy

## Grounding Context

**Strategy (verbatim approach):** "Win on privacy and performance. Local-first capture with capture-time filtering and per-app consent baked in, low enough overhead to run all day, with rich enough signal (mouse, keyboard, window, network, audio) that the same recording serves the user *and* can be opted into a training corpus for computer-use agents. The consumer product is the wedge; the data flywheel is what makes the strategic case." (Crux flagged soft, pending competitive research — only a ScreenPipe brief exists.)

**Active tracks:** (1) Capture engine quality & performance — *the floor*; (2) Privacy & trust; (3) UX & native experience — *load-bearing this quarter*; (4) Replay, MCP & data flywheel. **Persona:** non-technical internal-tool-heavy operators (CSMs, ops analysts, sales engineers) across 8–15 internal dashboards. **Not working on:** Windows.

**Key metrics:** daily active recording hours/user; capture reliability ≥98%; CPU/mem p95; privacy incidents per 1k recordings (~0); % users opting traces into training corpus (n/a until upload ships).

**Central strategic tension (the throughline for selection):** the last ~30 commits and most open tickets are capture-engine "floor" reliability work (SCR-101/103/108 capture-health), while the quarter's stated load-bearing track (UX/native) and the strategy-defining data-flywheel/upload pipeline are under-invested (brainstorm-only, no tickets), MCP is named in the strategy but absent from the backlog, and the crux is explicitly soft.

**Codebase (background):** Daemon (HTTP over UNIX socket, auto-spawn idle-shutdown). Pipeline: reader threads → event_q → processor → type-queues → writers. Capture via `screencapture` CLI (mss `CGWindowList` throttled ~30s on Sequoia). Two-level fail-closed privacy (capture-time filter + post-record scrub). SwiftUI shell drives the Python CLI as a subprocess. Action-gated video. Raw `sqlite3` catalog. `ax_cache.py` / `ax_browser_url.py` already query the accessibility tree on-demand. `metrics.py` collects per-recording system metrics.

**Past learnings (background):** TCC permissions + the SwiftUI↔Python bridge are the dominant recurring pain (5/16 solution docs; per-process TCC cache → Quit & Relaunch; no sub-binary TCC). Capture-health invariant: only an authoritative `screen_recording` denial may terminally stop a recording. Daemon EventBus has explicitly-deferred work. Cloud/upload reliability is deep but in flux (GCP migration zkairdrop→proteus-photos). Video encode budget is tight (~1.4ms headroom/frame at 3K).

**External (background):** Rewind.ai shut down Dec 2025 (Meta acquisition) → vacuum for local-first macOS screen-memory. ScreenPipe is the closest prior art (event-driven, accessibility-tree text + OCR fallback, 5–10 GB/mo). Microsoft Recall: <10% activation; enclave trust-boundary breached (TotalRecall) — opt-out framing + privacy friction kills adoption. Agent-training formats (CUA-Suite/VideoAgentTrek): step = (screenshot, observation, reasoning, action, pyautogui_code, result); accessibility-tree bounding boxes >> raw screenshots; spatial grounding only 47.7%. On-device PII: OCR→NER→redact-before-persist in the hot path. macOS API: `CGWindowListCreateImage` obsoleted in macOS 15 → migrate to ScreenCaptureKit; Sequoia monthly re-confirm; Tahoe drops non-bundled executables from the Privacy UI. Cross-domain: dashcam event-triggered retention; observability collector (redact in hot path); flight-data-recorder (retention windows + playback).

## Topic Axes

- A1. Capture engine: quality & performance
- A2. Privacy & trust
- A3. UX & native experience (load-bearing this quarter)
- A4. Replay, MCP & data flywheel
- A5. Strategy clarity & measurement

## Ranked Ideas

### 1. Adopt the accessibility (AX) tree as a first-class capture stream
**Description:** Today `ax_cache.py` queries the AX hierarchy on-demand at click time and discards it. Instead, snapshot the focused-window AX subtree (roles, labels, values, bounding boxes) on every action event and persist it as a new event type alongside `screen.frame`, written once through the existing reader→event_q→writer pipeline.
**Axis:** A1
**Basis:** `direct:` AX plumbing already exists (`ax_cache.py`, `ax_browser_url.py`) — this changes *retention*, not acquisition. `external:` agent-training research ranks accessibility-tree bounding boxes + action traces far above raw screenshots; top-model spatial grounding on pixels is only 47.7%.
**Rationale:** The canonical leverage move — one unit of capture work pays into four tracks: richer signal (A1), pixel-accurate redaction targets (A2), structured replay search (A4), and the single highest-value training signal (A4).
**Downsides:** Storage/schema growth; per-action AX snapshotting has a CPU cost that must respect the tight frame budget; AX coverage is uneven across apps.
**Confidence:** 85%
**Complexity:** Medium-High
**Status:** Unexplored

### 2. Native pre-upload review + reversible consent surface (with redaction preview)
**Description:** Build the brainstormed SwiftUI review window: from the Recordings list, the operator scrubs a recording, sees what redaction did and didn't catch, and consents or cancels — shelling out to the existing `screencap upload`. Consent is reversible until bytes actually leave the device.
**Axis:** A4 (doubles as the A3 trust surface)
**Basis:** `direct:` the upload-review brainstorm (`docs/brainstorms/2026-05-27-upload-review-screen-requirements.md`) quotes the shell's current terminal-only retry ("Run `screencap upload`"), and `upload.py` already exists — this is blocked purely on UX.
**Rationale:** The literal junction where the consumer wedge cashes into the data flywheel, and the only path to move the "% opting into training corpus" metric (currently n/a). Capture-health got three tickets; this got zero.
**Downsides:** Depends on the redaction story being trustworthy enough to show its own misses; cloud pipeline is mid-migration (zkairdrop→proteus-photos).
**Confidence:** 85%
**Complexity:** Medium
**Status:** Unexplored

### 3. Instrument the strategy's own key metrics + a consent-intent leading indicator
**Description:** A lightweight local rollup emitting daily-active-recording-hours, capture-reliability %, CPU/mem p95, and privacy-incident counts — plus a consent-intent counter (how many users would-toggle "contribute to training") that works *before* upload ships.
**Axis:** A5
**Basis:** `direct:` `metrics.py` already collects per-recording system metrics; STRATEGY says metrics are "measured from the catalog DB and local telemetry" — but it's unclear those numbers can actually be produced today.
**Rationale:** Cheapest high-leverage move. Makes the soft crux measurable and every future track-weighting decision evidence-based; tests the flywheel conversion assumption before it's funded.
**Downsides:** Pre-upload intent may not predict post-upload behavior; risks being treated as "done" without anyone acting on the data.
**Confidence:** 86%
**Complexity:** Low-Medium
**Status:** Unexplored

### 4. Native searchable / faceted replay timeline
**Description:** A native timeline with search/faceting over the window/app/network event tables already in SQLite — "show me everything in Salesforce on Tuesday afternoon involving invoice PDFs" — scoped to local-only retrieval. There is no `search` command today and replay lives only in a static HTML viewer.
**Axis:** A4
**Basis:** `direct:` grep shows no search command in the CLI, replay is only `viewer/html.py`. `external:` Rewind.ai's Dec-2025 shutdown opened a local-first macOS screen-memory vacuum; faceted retrieval matches multi-dashboard work.
**Rationale:** The consumer wedge's actual payoff. Today data goes in but effectively can't come back out for the non-technical persona — the wedge has no value loop.
**Downsides:** Faceted native UI is substantial; search quality depends on signal richness (compounds with #1).
**Confidence:** 82%
**Complexity:** Medium-High
**Status:** Unexplored

### 5. TCC self-heal + zero-click onboarding to proof-of-capture
**Description:** Detect the stale-permission state (macOS caches TCC per-process, forcing Quit & Relaunch), drive the relaunch automatically, deep-link the right Settings pane, and end onboarding at a concrete "here's the 10-second sample I just captured" moment — not a permissions checklist.
**Axis:** A3 (load-bearing this quarter)
**Basis:** `direct:` the per-process-TCC-cache and ad-hoc-signing learnings are the single most-documented friction (5/16 solution docs); `CGPreflightScreenCaptureAccess` already exists.
**Rationale:** Activation is the gate on every downstream metric — MS Recall's <10% activation shows a recorder users can't trust on day one is dead on arrival. Directly serves "install in two clicks, reachable for non-technical operators."
**Downsides:** Some TCC behavior is an immovable OS boundary (no sub-binary TCC); Sequoia's monthly re-confirm is a recurring tax, not a one-time fix.
**Confidence:** 88%
**Complexity:** Medium
**Status:** Unexplored

### 6. Hot-path, AX-field-keyed redact-before-persist
**Description:** Move redaction into the writer path *before* anything hits disk, using the AX tree's field semantics (a `secureTextField`, a field labeled "SSN") to blur the exact bounding box — rather than OCR'ing every frame after the fact. Fail-closed if the redactor can't keep up.
**Axis:** A2
**Basis:** `external:` on-device redact-before-persist in the hot path is the emerging bar; `direct:` `scrubber.py`'s `run_chunk()` and the two-level fail-closed model already exist.
**Rationale:** "Win on privacy" only holds if PII never reaches disk unredacted — and that's the precondition for any corpus opt-in being defensible. Leverages #1's AX tree for near-free, pixel-accurate targeting.
**Downsides:** The ~1.4ms/frame encode headroom at 3K makes hot-path NER genuinely hard — may force a deferred queue; correctness-critical.
**Confidence:** 80%
**Complexity:** High (depends on #1)
**Status:** Unexplored

### 7. ScreenCap as a local MCP server (agent-memory retrieval)
**Description:** Expose the SQLite catalog as a local MCP server so any agent (Claude Desktop, Cursor, internal copilots) can query "what dashboards did I touch this morning" or "find the screen where the invoice total was wrong." The daemon already speaks HTTP over a UNIX socket, so the MCP surface is a thin adapter.
**Axis:** A4
**Basis:** `direct:` existing daemon `/v0/*` API + raw sqlite catalog; `reasoned:` MCP is named in the strategy's flywheel track but absent from the entire backlog.
**Rationale:** Activates a named-but-missing track with a small build, makes the recording actionable in the operator's existing tools, and is the bridge to the agent-builder buyer without a separate product.
**Downsides:** Good agent-facing query/tool design is real work; the redaction-on-read story must be solid before exposing screen history to an agent.
**Confidence:** 78%
**Complexity:** Medium
**Status:** Unexplored

## Cross-Cutting Note

A **versioned structured trace schema** (CUA-Suite-shaped: screenshot · observation · action · target-element · result) is the connective substrate beneath #1, #4, #6, and #7 — worth defining explicitly when brainstorming any of them rather than letting each consumer re-derive structure. A natural sequencing falls out: **#3 (measure)** is the cheap do-first; **#1 (AX capture)** is the foundation that unlocks #4/#6/#7; **#2 + #5** are the load-bearing UX/flywheel moves the quarter calls for.

## Rejection Summary

| # | Idea | Reason Rejected |
|---|------|-----------------|
| 1 | Structured-only retention tier (drop pixels after N days) | Downstream of #1's structured layer; revisit after AX capture lands |
| 2 | Performance as enforced contract / overhead auto-degrade | Overhead already actively worked (idle-backoff/capture-health); lower urgency than the UX/flywheel gap |
| 3 | Chain-of-custody ledger / SBOM provenance | Premature — makes the corpus *sellable*, but the corpus doesn't exist yet |
| 4 | Instant-replay segmentation into trajectories | Fold into #4 / the training exporter; not a standalone bet |
| 5 | Sealed-exhibit reversible redaction (court-reporter) | Key-management burden disproportionate to stage; brainstorm variant of #6 |
| 6 | Single "capture profile" abstraction | Internal refactor — engineering-todo, not a strategy-visible bet |
| 7 | Self-describing capture-health event / "why did it stop?" | Incremental polish on the already-heavily-worked floor; fold into #2/#5 |
| 8 | Pick-the-buyer / forced wedge decision + competitive research | Strategy-doc decision (route to ce-strategy / competitive brief); buildable part is #3 |
| 9 | Federated / local-only corpus export | Premature + research-heavy; brainstorm variant once #2 (consent) exists |
| 10 | Split recording into two assets | Absorbed into #1 (AX as distinct asset) + #7 (reads structured layer) |
| 11 | Versioned trace schema (standalone) | Connective substrate beneath #1/#4/#6/#7; surface within those, not separately |

_Axis coverage: all five axes have a survivor; no deliberate gaps._
