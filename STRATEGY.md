---
name: Screencap
last_updated: 2026-05-08
---

# Screencap Strategy

## Target problem

Internal-tool-heavy operators — customer success managers, ops analysts, sales engineers — reason across 8-15 internal dashboards and tools every day, and that reasoning is currently lost. They can't replay it, can't share it with teammates, and can't hand it to AI agents that could automate parts of the workflow. Existing recall tools fail on three axes at once: they upload everything to the cloud (privacy non-starter for company data), capture too little signal (screenshots only — no keyboard, window, or network context), and are too heavy to run all day.

> _Section flagged for revision after competitive research (ScreenPipe, Dayflow, others). The crux statement is still soft._

## Our approach

Win on privacy and performance. Local-first capture with capture-time filtering and per-app consent baked in, low enough overhead to run all day, with rich enough signal (mouse, keyboard, window, network, audio) that the same recording serves the user *and* can be opted into a training corpus for computer-use agents. The consumer product is the wedge; the data flywheel is what makes the strategic case.

> _Section flagged for revision after competitive research._

## Who it's for

**Primary:** The internal-tool-heavy operator — customer success managers, ops analysts, sales engineers, support leads working across many internal dashboards. They're hiring Screencap to stop losing the thread when switching between Salesforce, the data warehouse, Looker, and internal admin panels — and eventually, to let an agent do the next round of the workflow for them.

## Key metrics

- **Daily active recording hours per user** — does the product fit into a real workday? Measured from the catalog DB and local telemetry. Weekly cadence.
- **Capture reliability rate** — % of recording sessions completing without crash, dropped frames, or DB corruption. Target ≥98%. Weekly cadence.
- **CPU / memory overhead at p95** — % CPU and MB RAM during active recording. Tests the "low enough to run all day" claim. Weekly cadence.
- **Privacy incidents per 1k recordings** — scrub misses, leaked titles in cloud-bound paths, PII in training exports. Must stay at or near zero. Sourced from privacy audit logs + CI guards. Monthly cadence.
- **% of users opting traces into the training corpus** — lagging signal that the data flywheel is forming. Tracked as `n/a` until the upload pipeline ships. Quarterly cadence.

## Tracks

### Capture engine: quality & performance

The recording engine itself — rich signal capture (mouse, keyboard, window, network, audio), reliability, and low overhead.

_Why it serves the approach:_ Without a recorder that's reliable and cheap to run all day, neither the consumer wedge nor the data flywheel can exist. This is the floor.

### Privacy & trust

Capture-time filtering, scrubbing pipeline, per-app consent UX, audit trails, fail-closed defaults.

_Why it serves the approach:_ The "win on privacy" half of the approach. Enterprise users won't install a recorder they don't trust, and the training corpus is only ethical if consent is real.

### UX & native experience  *(load-bearing this quarter)*

SwiftUI macOS shell, install flow, onboarding, and a UI that's compelling enough to hold its own against Dayflow and others. Make the product reachable for non-technical operators, not just CLI users.

_Why it serves the approach:_ The persona is non-technical operators. Without a UI they trust and can install in two clicks, the consumer wedge doesn't exist — and without the wedge, the data flywheel never starts.

### Replay, MCP, & data flywheel

Turn raw recordings into something usable: searchable timeline, workflow replay, MCP surface for computer-use agents, opt-in upload, segmentation, training corpus formation.

_Why it serves the approach:_ This is where both the user-facing JTBD ("let an agent do the next round") and the strategic bet (enterprise training data) get cashed in. Folded into one track because they share infrastructure and represent two outputs of the same underlying capability.

## Not working on

- **Windows.** Started a Windows version and dropped it for now. macOS only until the macOS product is working for the target persona.
