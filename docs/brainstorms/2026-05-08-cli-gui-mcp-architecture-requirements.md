---
date: 2026-05-08
topic: cli-gui-mcp-architecture
---

# CLI / GUI / MCP Architecture: Daemon-Shaped Engine

## Summary

ScreenCap will refactor toward a single long-lived `screencap serve` daemon (per-user `LaunchAgent`) that owns the recording engine, with the CLI, the SwiftUI app, and a future MCP server all becoming thin clients of one local API. The change is staged in two phases — Phase 1 extracts the API and routes the SwiftUI shell through it; Phase 2 consolidates the engine into the daemon and lands MCP as a separate-process client.

---

## Problem Frame

ScreenCap today runs a CLI-as-engine + SwiftUI-shells-CLI architecture: the SwiftUI app spawns the bundled `screencap` binary as a long-lived subprocess for recordings and short-lived subprocesses for `status`/`list`, parsing versioned JSON on stdout and line-buffered structured events on stderr. This shape is a textbook subprocess-per-call pattern — the same one Tailscale, Docker, Ollama, Syncthing, rclone, and Mullvad all started with and refactored away from once a third surface arrived.

The third surface is now arriving. The strategy doc treats UX & native experience as load-bearing this quarter, and MCP for computer-use agents is a near-term commitment, not a long-horizon bet. With three surfaces (CLI, GUI, MCP) needing to drive the same recording engine, the subprocess pattern stops scaling: MCP cannot meaningfully query a *live* recording session through per-call subprocesses, the GUI and MCP would otherwise see two divergent contracts (live stderr stream vs. catalog-DB reads), and TCC grants tied to an ad-hoc-signed SwiftUI build keep getting clobbered on rebuild.

The cost of staying on the current shape is paid in two places. First, every new agent-driven verb (pause, query metadata mid-recording, switch privacy profile live) becomes a special case with its own IPC mechanism. Second, the SwiftUI shell carries TCC permissions it doesn't need to carry — Screen Recording, Accessibility, Input Monitoring — which is a problem the moment the daemon could carry them on a stable signing identity instead.

---

## Actors

- A1. **Power-user / scriptable CLI caller**: invokes `screencap` from a terminal or shell script. Cares about command exit codes, JSON output, and being able to run things headlessly without the GUI installed or open.
- A2. **Non-technical operator (GUI user)**: launches the SwiftUI app, records, browses recordings. Per the strategy doc, this is the primary persona. Cares about install friction, "it just works," low daily-use overhead.
- A3. **Computer-use agent (MCP client)**: a remote or local agent connected via MCP that needs to start, query, pause, and stop recordings, and read recording metadata while a session is live. Cares about a stable typed contract and live event streaming.
- A4. **ScreenCap engineer**: maintains the engine, the API contract, and the daemon. Cares about contract stability, debuggability of the local socket, and not having two divergent IPC paths to keep in sync.

---

## Key Flows

- F1. **GUI user records, agent observes**
  - **Trigger:** A2 clicks Record in the SwiftUI app while A3 is connected via MCP.
  - **Actors:** A2, A3.
  - **Steps:** SwiftUI calls the daemon's `start-recording` verb → daemon begins capture → daemon emits live events → SwiftUI updates banner, MCP server forwards events to the agent.
  - **Outcome:** Both surfaces see the same session through the same contract; neither has a privileged view.
  - **Covered by:** R1, R3, R4, R7.

- F2. **Agent starts recording, GUI joins late**
  - **Trigger:** A3 starts a recording via MCP; A2 launches the SwiftUI app afterwards.
  - **Actors:** A3, A2.
  - **Steps:** MCP server calls `start-recording` → daemon begins capture → SwiftUI launches, queries `status`, sees recording in progress, subscribes to live events, renders the same UI as if it had started the session.
  - **Outcome:** Late-joining surfaces reconcile with live state without restart or special handling.
  - **Covered by:** R1, R3, R4, R7.

- F3. **CLI power user, no GUI installed**
  - **Trigger:** A1 runs `screencap record …` on a machine where the SwiftUI app is not running or not installed.
  - **Actors:** A1.
  - **Steps:** CLI detects no daemon → either starts the daemon transparently or runs in-process (Phase 1) → records → exits cleanly.
  - **Outcome:** Headless and CI-style usage continues to work without requiring the GUI.
  - **Covered by:** R2, R6, R10.

- F4. **First-launch install on a fresh machine**
  - **Trigger:** A2 installs the SwiftUI app and launches it for the first time.
  - **Actors:** A2.
  - **Steps:** App installs the `LaunchAgent` plist → daemon starts → app prompts for required TCC grants against the daemon's bundled binary → grants persist across app rebuilds because the daemon's signing identity is stable.
  - **Outcome:** TCC grants survive subsequent SwiftUI rebuilds; the user is not re-prompted on every dev build.
  - **Covered by:** R5, R8, R9.

---

## Requirements

**Architecture shape**
- R1. Recording engine, catalog access, privacy filter pipeline, and event stream all live inside a single long-lived daemon process. No surface holds engine state of its own beyond display caches.
- R2. The same `screencap` binary serves CLI commands, daemon mode (`screencap serve`), and any future modes via mode dispatch. No separate binaries.
- R3. CLI, SwiftUI app, and MCP server are all clients of one local API exposed by the daemon. The same verbs and event stream are available to all three.
- R4. The API supports both request/response calls and a persistent event-subscription stream. Live events (recording started, frame written, privacy hit, etc.) are observable by any connected client.

**Surfaces**
- R5. The SwiftUI app does not require Screen Recording, Accessibility, or Input Monitoring TCC grants. Those grants are held by the daemon's bundled binary on its stable signing identity.
- R6. CLI commands continue to work in environments where the SwiftUI app is not installed, not running, or not launched. The daemon is the only required component for headless use.
- R7. MCP server runs as a separate process connecting to the same local socket as the other surfaces. MCP is not embedded in the daemon and not embedded in the SwiftUI app.

**Lifecycle**
- R8. The daemon runs as a per-user `LaunchAgent`. It is not a system `LaunchDaemon`; no privileged operations are performed.
- R9. The daemon starts on user-session login (via `LaunchAgent`) and persists across SwiftUI app launches and quits. Idle state is intentionally cheap; auto-shutdown on idle is a non-goal.
- R10. The daemon supervises itself enough to recover from an engine crash mid-recording without losing previously written frames, events, or catalog rows. Specific recovery semantics are planning territory.

**Migration**
- R11. Phase 1 lands the daemon and the API contract, routes the SwiftUI shell through the API, and leaves CLI commands working in-process. Stderr-event streaming for the SwiftUI shell continues to work during Phase 1 as a transitional path.
- R12. Phase 2 moves the recording engine into the daemon, makes CLI commands that touch live state into API clients, and adds the MCP server. After Phase 2, stderr events become an internal implementation detail of the daemon and are not part of any external contract.
- R13. Each phase ships behind its own release. Phase 1 is reversible (the daemon is additive); Phase 2 is the consolidation step.

**Contract design**
- R14. The API contract is designed with MCP-driven verbs in mind from Phase 1. Verb selection and event-stream shape must satisfy agent-driven use cases (start, query, pause, stop, observe) without later contract breaks.
- R15. API versioning re-uses the existing per-endpoint schema-version pattern from `cli.py`. No new versioning scheme is introduced for the daemon API.

---

## Acceptance Examples

- AE1. **Covers R3, R4, R7.** Given the daemon is running and the SwiftUI app has just started a recording, when an MCP client queries the live session, the client sees the same session ID, start time, and live event stream that the SwiftUI app sees.

- AE2. **Covers R5, R8.** Given the SwiftUI app is rebuilt locally during development, when the user re-launches the rebuilt app, the daemon's TCC grants for Screen Recording, Accessibility, and Input Monitoring remain in effect — the user is not prompted to re-grant.

- AE3. **Covers R6, R9.** Given the SwiftUI app is not installed on the machine, when the user runs `screencap record …` from a terminal, the recording starts, completes, and exits cleanly without any GUI process being launched.

- AE4. **Covers R11, R12.** Given Phase 1 has shipped but Phase 2 has not, when the SwiftUI app issues a recording command, the engine work happens through the API contract; when a CLI user issues `screencap record …`, the engine work happens in-process. Both produce equivalent recordings.

- AE5. **Covers R10.** Given a recording is in progress and the daemon's recording engine crashes, when the daemon restarts, the previously written frames and catalog rows for that session are intact and the user is informed that the session ended early.

---

## Success Criteria

- A non-technical operator (A2) can install the SwiftUI app on a fresh macOS machine and produce a recording without re-granting TCC permissions across subsequent app updates.
- An agent (A3) can start, query, pause, and stop a recording over MCP, observing the same live state the GUI would see, without the GUI being open.
- A power user (A1) running `screencap record …` from a terminal continues to work after Phase 2 without the SwiftUI app being installed.
- A future ScreenCap engineer (A4) implementing a new verb adds it once on the daemon API and gets CLI / GUI / MCP support automatically — no per-surface plumbing.
- The downstream `ce-plan` run for Phase 1 does not need to re-invent which surfaces talk to which contract, what runs in-process vs. in the daemon, or what TCC grants live where.

---

## Scope Boundaries

- Approach A (minimal session bus on top of the current architecture) and Approach C (GUI-as-engine, OBS / 1Password shape) — surveyed against this design space and rejected.
- Cross-platform daemon design. macOS-only, per the strategy doc's "not working on Windows" decision.
- Remote access to the daemon over TCP or any network socket. Local Unix socket only; remote control is a separate product decision, not an architectural one.
- Auth tokens, ACLs, or capability-based permissions on the socket beyond a basic same-user origin check. The socket's filesystem permissions are the authorization surface for v1.
- Rewriting the Python engine in Swift, or making the SwiftUI app the engine. The recording engine stays in Python; only the *invocation pattern* changes from per-call subprocess to long-lived daemon.
- Distribution decisions (App Store vs. direct `.pkg`, Homebrew tap, Sparkle updater integration). Architecture-adjacent, but a separate brainstorm.
- Specific verb names, JSON field names, error message text, socket path conventions. That is planning and design territory, not requirements.
- Backwards compatibility shims for the stderr-event contract beyond Phase 1. After Phase 2, stderr events are internal to the daemon.
- Engine swap-out (e.g., a Swift-native capture engine running in-process). Possible later; out of scope for this brainstorm.
- Auto-shutdown of the idle daemon. "Run all day" is the strategy; the daemon staying resident is a feature.

---

## Key Decisions

- **Daemon over GUI-as-engine.** The strategic bet on computer-use agents requires that automation work without the GUI being open. GUI-as-engine (OBS / 1Password posture) would foreclose that. Rationale: research showed every durable 3-surface tool consolidates on either GUI-as-engine or daemon, and ScreenCap's strategy points at daemon.

- **`LaunchAgent`, not `LaunchDaemon`.** Screen recording requires the user's GUI session and prompts via TCC, neither of which a system `LaunchDaemon` can do. A per-user `LaunchAgent` matches every macOS recorder precedent and avoids the Docker-Desktop-4.15 anti-pattern of a permanent privileged process.

- **Local HTTP over Unix socket.** Chosen over gRPC, Mach service, and XPC. The pattern matches Tailscale's LocalAPI, Docker's `/var/run/docker.sock`, Ollama's `:11434`, Syncthing's REST, and rclone's RC API. Debuggable from `curl`, language-agnostic, well-supported by Swift, Python, and any MCP server runtime.

- **MCP server as a separate process.** Mirrors Ollama's pattern where MCP servers translate MCP tool calls into HTTP/RPC calls against the engine's existing API. Keeps the daemon free of MCP library coupling and lets the MCP surface be replaced or run remotely without touching the engine.

- **Two-phase rollout.** Extract the API contract first (Phase 1), consolidate the engine after (Phase 2). The biggest risk in this refactor is locking in the wrong verbs; staging makes the contract observable in production before the engine consolidation makes it irreversible.

- **TCC grants migrate to the daemon's bundled binary.** Side effect: the SwiftUI shell stops carrying screen-recording grants on an ad-hoc dev signature, which solves the rebuild-clobbers-grants pain documented in `CLAUDE.md`. The daemon's PyInstaller bundle has a stable signing identity; SwiftUI's dev signature drift no longer matters for permissions.

- **No auto-shutdown on idle.** The strategy positions ScreenCap as "low enough to run all day." A daemon that idles cheaply is on-strategy; one that constantly tears down and re-prompts for TCC is not.

---

## Dependencies / Assumptions

- The PyInstaller-bundled `screencap` binary can be code-signed with a stable Developer ID for distribution. This is independent of architecture but is the prerequisite for stable TCC grants on the daemon. Not yet validated end-to-end in this codebase — verify during Phase 1 planning.

- macOS 13+ as the deployment floor (per `macos/project.yml`). All architectural choices assume this floor and do not need to support older macOS versions.

- The screen-capture path remains `screencapture` CLI on macOS (per the existing `MEMORY.md` decision about `mss` being throttled on Sequoia). The daemon shape does not change this; `screencapture` works from any thread/process.

- Recording is exclusive — only one active session at a time. The API contract does not need to model concurrent sessions in v1.

- The existing per-endpoint JSON schema-version pattern in `cli.py` is the right reuse target for the daemon API. Verified by inspection of `cli.py`; the pattern already exists.

- MCP being a near-term commitment is treated as load-bearing for this design. If MCP slips materially, the architectural urgency drops but the design doesn't become wrong — just less time-pressured.

---

## Outstanding Questions

### Resolve Before Planning

- *(none — synthesis was confirmed; all scope-shaping decisions are in Requirements, Key Decisions, or Scope Boundaries.)*

### Deferred to Planning

- [Affects R3, R4][Technical] What's the minimal verb set the daemon API must expose in Phase 1 to satisfy the SwiftUI app's current usage without locking out MCP-driven verbs? Surface verbs needed by F1–F3 should anchor the answer.
- [Affects R4][Technical] Streaming protocol on the API — Server-Sent Events, chunked JSON, WebSocket, or newline-delimited JSON over the socket? All four work; the choice depends on Swift / Python / MCP-runtime ergonomics.
- [Affects R8, R9][Technical] `LaunchAgent` plist placement, ownership, and install path. Conventions exist; pick one consistent with the SwiftUI app's bundle ID.
- [Affects R10][Needs research] Crash recovery semantics — what's the minimum viable behavior when the daemon dies mid-recording? Survey what existing macOS recorders do (none of them are agent-friendly, so the bar is set by general daemon-supervision best practice rather than category precedent).
- [Affects R5][Needs research] What does the TCC grant migration look like for existing users on the current architecture? Is there a clean handoff, or do users re-grant once at upgrade time?
- [Affects R7][Technical] MCP server packaging — same Python interpreter as the daemon (PyInstaller bundle) or independent? Same bundle reduces install footprint; independent allows the MCP server to be swapped or remote-hosted.
- [Affects R14][Technical] Origin / caller-identity check on the socket. Filesystem perms are the v1 authorization surface, but should the daemon also reject connections from unexpected callers (e.g. process-name check)? Possibly trivial, possibly a rabbit hole.
