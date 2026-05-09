---
title: "Stderr-bridge backpressure stalls engine on emit_event when subscriber is slow"
status: open
priority: high
created: 2026-05-09
related_plans:
  - docs/plans/2026-05-08-001-feat-daemon-architecture-phase-1-plan.md
  - docs/plans/2026-05-08-002-feat-daemon-architecture-phase-2-plan.md
related_review_run: /tmp/compound-engineering/ce-code-review/20260508-193011-a0d26cbf/
---

# Stderr-bridge backpressure stalls engine on emit_event when subscriber is slow

## Problem

The engine writes lifecycle events through `_stderr_events.emit_event` (calls `sys.stderr.write` + flush per event). The daemon's `Supervisor._stderr_pump` task drains the engine subprocess's stderr pipe and republishes onto the bus. Two backpressure paths converge on the kernel stderr pipe:

1. **Bus → bridge.** A slow subscriber's per-subscriber asyncio queue fills (capped at 1024 events). Once full, that subscriber gets closed with `slow_consumer` — but until that close fires, `EventBus.publish` is `await`ing the `put` per subscriber, which gates the bridge's per-line republish.
2. **Bridge → engine.** While the bridge is blocked publishing, it's not draining stderr. The kernel pipe (~16 – 64 KB on macOS by default) fills. The next `sys.stderr.write` from the engine **blocks on the kernel** — not just the bridge.

Net effect: a slow subscriber transitively blocks the engine. The 1Hz `proc.is_alive()` poll reports `running` (write-blocked ≠ dead), so the supervisor never notices. Frame-write events keep failing to emit, and the engine itself stops doing useful work whenever it tries to log.

The original Tier-2 adversarial reviewer flagged this at supervisor.py:~400 (the pump task). The actual emit site is [src/screencap/_stderr_events.py:66](../../src/screencap/_stderr_events.py) (`emit_event`); the load-bearing pump site is in [src/screencap/daemon/supervisor.py](../../src/screencap/daemon/supervisor.py) (the `_stderr_pump` async method). Both ends matter.

**Phase 2 amplifies this.** When MCP becomes a third event consumer, the slow-consumer probability grows — agents do non-trivial work between event reads, and a stuck MCP client ties up bus-publish time the bridge needs.

## What's needed

Pick a fix and ship it. The candidates, in increasing order of invasiveness:

1. **Widen the kernel pipe.** Call `fcntl.fcntl(stderr_fd, F_SETPIPE_SZ, 1 << 20)` (1 MiB) on the supervisor side after `subprocess.Popen` returns. Buys ~16x more headroom before the engine blocks. Shortest patch; doesn't fix the root issue (slow subscriber still backs up the bridge), just delays it. **This alone is probably enough for v1** given Phase 1's subscriber count is 1–2.
2. **Decouple the bridge from publish.** Insert an unbounded internal buffer between stderr-readline and bus-publish (a dedicated `asyncio.Queue` the pump fills and a fan-out task drains). Slow subscribers no longer block the bridge from draining stderr; they only delay their own delivery. Pair with a pump-side drop policy if the buffer grows past N events (say 10K) — log a warning, drop oldest, never block stderr.
3. **Move the engine-to-supervisor channel off stderr entirely.** Use a dedicated `os.pipe()` pair the engine writes to. Stderr stays for human-readable diagnostics. Cleanest separation of concerns. Most invasive — touches `_stderr_events.emit_event`, the engine spawn path, and the pump.

Recommended for this ticket: do (1) immediately as a safety net, then evaluate (2) if measurement shows the bridge backs up under realistic load. Skip (3) unless the engine-topology spike (`docs/tickets/2026-05-08-engine-topology-spike.md`) lands on a topology where it falls out naturally.

Whichever fix lands, add a regression test that:
- Creates a subscriber and pauses its queue drain.
- Publishes >1024 events through the bus while observing that the bridge keeps draining stderr (e.g. by seeing a sentinel event at the back of the test stream arrive at a fast subscriber).
- Asserts the slow subscriber gets closed with `slow_consumer` without the bridge stalling.

## Acceptance

- A scripted test (or manual smoke documented in the ticket) demonstrates the engine continues to emit events through the bridge while one subscriber is paused at >1024 events of backlog.
- `_stderr_pump` does not block `>500ms` on any single bus-publish call (assertable via instrumentation in the test).
- Engine-side `emit_event` does not block on stderr write under the same scenario (verifiable by timing a sentinel event end-to-end).
- If approach (1) is taken: the `F_SETPIPE_SZ` call is documented with the buffer size chosen and the rationale for that size.

## References

- Tier-2 ce-code-review run artifact: `/tmp/compound-engineering/ce-code-review/20260508-193011-a0d26cbf/`
- Reviewer: adversarial (ADV-1, confidence 75, P1)
- Related learning: [docs/solutions/runtime-errors/launchd-plist-tilde-expansion-2026-05-09.md](../solutions/runtime-errors/launchd-plist-tilde-expansion-2026-05-09.md) — different finding, same architectural area (daemon ↔ engine IPC).
- Related ticket: `docs/tickets/2026-05-08-engine-topology-spike.md` — if the spike picks "in-process engine," this entire bridge disappears and approaches (2)/(3) become moot.
