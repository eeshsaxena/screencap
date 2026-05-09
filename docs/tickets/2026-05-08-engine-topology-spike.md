---
title: "Spike: engine topology — in-process vs subprocess in the daemon"
status: open
priority: medium
created: 2026-05-08
related_plans:
  - docs/plans/2026-05-08-001-feat-daemon-architecture-phase-1-plan.md
  - docs/plans/2026-05-08-002-feat-daemon-architecture-phase-2-plan.md
related_brainstorms:
  - docs/brainstorms/2026-05-08-cli-gui-mcp-architecture-requirements.md
---

# Spike: engine topology — in-process vs subprocess in the daemon

## Why this exists

Origin brainstorm requirement R1 says the recording engine "lives inside a single long-lived daemon process." Phase 1's plan deliberately rejected the in-process reading on crash-isolation grounds — "every engine SEGV would drop the API; upgrade-during-recording impossible" — and chose a launchd → daemon → engine subprocess topology. Phase 2 honors R12 ("engine consolidates into the daemon") by closing CLI's spawn path so the daemon is the sole engine spawner, but does **not** touch the supervisor topology.

The "in-process vs subprocess" decision is a real architectural trade-off that benefits from measurement, not paper analysis. This ticket captures the spike work needed to settle it.

## What needs answering

The decision boils down to: does the in-process simplification justify the loss of crash isolation, given measured behavior on this codebase?

**Option A — Truly in-process engine.** Engine runs in the daemon's event loop / thread pool. R1's literal reading honored. Removes Phase 1's supervisor + stderr-bridge complexity. But: every engine SEGV drops the daemon (relies on launchd KeepAlive to recover, ~5-10s gap); upgrade-during-recording impossible without dropping the recording; recording state lost between crash and restart.

**Option B — Keep subprocess, tighten ownership.** Phase 2's current shape. Engine remains a `multiprocessing.spawn` child of the daemon. Crash isolation preserved. Stderr events are daemon-internal but the IPC mechanism remains. R1's "single daemon process" stays a logical statement, not a literal one.

## What to measure

Before committing to either path, gather:

- **Idle daemon CPU and RSS** with engine-in-process vs engine-as-subprocess on the frozen PyInstaller binary on a quiet machine. If idle cost differs by >50%, that informs the decision.
- **GIL contention during active recording** — high-frequency events (`frame_written`, `chunk_finalized`) emit at the frame rate. In-process means engine and API request handling share the GIL. Measure p95 API request latency during active recording for both topologies.
- **Crash recovery latency** — kill the engine (SIGSEGV-equivalent via `kill -SEGV`) and measure: (a) how long until subscribers see a `recording_finalized(force_stopped=true)` event in Option B, (b) how long until the daemon is reachable again in Option A (launchd KeepAlive cycle).
- **Upgrade-during-recording semantics** — `launchctl kickstart` with active recording. Option A: recording is lost. Option B: orphan reconciliation finalizes catalog row. Confirm both behaviors empirically.
- **Engine subprocess spawn cost** — how much wall-clock latency does `multiprocessing.spawn` add to `recording.start`? If it's >500ms, the API contract may need an "ack-then-spawn" shape regardless of topology choice.

## Constraints to honor

- Phase 2's API contract must not change as a result of this spike — three independent clients (SwiftUI, CLI, MCP) are validated against the current contract. The spike concerns process topology, not API shape.
- macOS 13+ floor; `multiprocessing.spawn` mode (per existing codebase decision); `screencapture` CLI path for screen capture (per `MEMORY.md` decision about `mss` throttling).
- Whatever topology wins must preserve AE5 (engine crash mid-recording → previously written frames intact, catalog row marked `terminated_unexpectedly`).

## Suggested approach

1. Implement a small toggle (`SCREENCAP_ENGINE_TOPOLOGY=in-process|subprocess`) in the daemon supervisor. Default subprocess (Phase 1's current shape).
2. Measure the four numbers above for both modes on a representative recording (5 minutes, normal load).
3. Write up findings as a `docs/solutions/architecture/` entry once decided.
4. If in-process wins: open a follow-up plan to consolidate. If subprocess wins: close this ticket as "resolved — Phase 1's decision stands."

## Out of scope for this ticket

- Changing the API contract.
- Re-litigating CLI / MCP / SwiftUI surface designs.
- Any work that's already covered by Phase 2 implementation units.

## References

- Origin: [docs/brainstorms/2026-05-08-cli-gui-mcp-architecture-requirements.md](../brainstorms/2026-05-08-cli-gui-mcp-architecture-requirements.md) R1 (engine in daemon process), R10 (crash recovery semantics).
- Phase 1 plan rejection rationale: [docs/plans/2026-05-08-001-feat-daemon-architecture-phase-1-plan.md](../plans/2026-05-08-001-feat-daemon-architecture-phase-1-plan.md) — Key Technical Decisions ("Supervision topology"), Alternatives Considered ("In-process engine").
- Phase 2 plan deferring the question: [docs/plans/2026-05-08-002-feat-daemon-architecture-phase-2-plan.md](../plans/2026-05-08-002-feat-daemon-architecture-phase-2-plan.md) — Scope Boundaries, Deferred Follow-Up Work.
