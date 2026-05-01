---
title: "macOS Foundation.Process + Pipe — four pitfalls hit while building the SwiftUI shell"
slug: macos-foundation-process-pipe-pitfalls
date: 2026-05-01
category: integration-issues
severity: high
problem_type: integration-issues
modules:
  - macos/ScreenCap/Controllers/CLIClient.swift
  - macos/ScreenCap/Controllers/PermissionController.swift
tags:
  - macos
  - foundation
  - process
  - pipe
  - swift
  - tcc
  - subprocess
  - macos-app-shell
symptoms:
  - "Long-running screencap subprocess hangs after writing >64KB to stdout"
  - "Final stderr line (e.g. recording_finalized, stopped) intermittently missing in the SwiftUI shell"
  - "Spawned helper binary sees all TCC permissions as denied even though the parent .app has them granted"
  - "1Hz polling that spawns subprocesses creates 50+ piled-up child processes within a minute"
root_cause: >
  Foundation.Process + Pipe on macOS has four non-obvious gotchas: stdout
  buffer fills if the host doesn't drain it, terminationHandler can fire
  before the last readabilityHandler delivery, sub-binaries spawned via
  Process.run() do not inherit the parent .app's TCC bundle identity, and
  any timer-driven Process spawn is a fork-bomb risk if the spawn rate
  outpaces subprocess exit time.
---

## Problem / Goal

Build the SwiftUI shell at `macos/` to drive the bundled `screencap` Python CLI as a subprocess. Smoke-testing the round-trip surfaced four distinct ways `Foundation.Process` + `Pipe` can quietly misbehave on macOS. None are documented prominently by Apple; all four bit during the same PR.

## The four pitfalls

### 1. Undrained stdout pipe deadlocks the child

`CLIClient.spawn()` originally only attached a `readabilityHandler` to stdout when the caller supplied an `onStdoutLine` callback. `RecorderController` doesn't supply one (it consumes structured events from stderr), so stdout had no reader. Python `screencap start` writes Rich-console banners and progress to stdout. Once the OS pipe buffer (~64KB) filled, the Python child blocked on `write()` and stopped emitting stderr events too. The SwiftUI shell saw no `started` / `chunk_finalized` / `recording_finalized` and assumed the recorder had wedged.

**Fix:** always attach a stdout readabilityHandler that at minimum reads `availableData`. The bytes can be discarded if no caller wants the lines — the *read itself* is what unblocks the child.

```swift
let stdoutBufferOpt: LineBuffer? = onStdoutLine.map { LineBuffer(handler: $0) }
stdoutPipe.fileHandleForReading.readabilityHandler = { handle in
    let data = handle.availableData
    if data.isEmpty { return }
    stdoutBufferOpt?.feed(data) // discarded if no caller handler
}
```

### 2. terminationHandler races readabilityHandler — final line lost

Apple does **not** document an ordering guarantee between the last `readabilityHandler` callback and `terminationHandler`. When the child writes its final structured event (`recording_finalized`, `stopped`) and then exits, the readabilityHandler may not have dispatched the bytes yet when terminationHandler fires.

The original termination handler nilled the readabilityHandler and flushed the buffer:

```swift
process.terminationHandler = { _ in
    stderrPipe.fileHandleForReading.readabilityHandler = nil
    stderrBuffer.flush()  // only flushes bytes already fed in
}
```

Bytes still sitting in the kernel pipe at that moment died with the FD. Result: PR3's Cmd+Q wait for the `stopped` event would block for the full 5-minute timeout because the event was emitted but discarded.

**Fix:** drain the pipe to EOF inside the termination handler. The child has already closed its end, so `readToEnd()` returns immediately with whatever is buffered.

```swift
process.terminationHandler = { _ in
    stderrPipe.fileHandleForReading.readabilityHandler = nil
    if let remaining = try? stderrPipe.fileHandleForReading.readToEnd(),
       !remaining.isEmpty {
        stderrBuffer.feed(remaining)
    }
    stderrBuffer.flush()
}
```

### 3. `Process.run()` sub-binaries lose the .app's TCC bundle identity

To work around the in-process TCC cache (see [TCC per-process cache](../runtime-errors/macos-tcc-per-process-cache-quit-and-relaunch.md)), an early attempt spawned the .app's own binary with a `--check-permission` argv shortcut so each query ran in a fresh process. The terminal smoke test worked perfectly — but inside the running .app, every spawned helper saw every permission as denied.

Cause: when `Process.run()` `exec`s a binary directly, macOS attributes the spawned child to the spawning process for TCC purposes, not to the .app bundle that owns the binary on disk. The helper sub-process doesn't inherit `com.screencap.macos`'s TCC identity. From terminal it inherited Terminal.app's permissions (which has everything); from inside the .app it inherited... nothing meaningful.

**Lesson:** if you need a fresh-process TCC query, you have to relaunch the entire .app via LaunchServices (`NSWorkspace.openApplication(at:)` or `/usr/bin/open`). There is no in-bundle shortcut.

### 4. Timer-driven Process spawn is a fork-bomb

The same broken attempt above hit a worse failure mode: a 1Hz timer spawned 4 helper subprocesses per tick. The helpers each took ~1.7s to exit (AVFoundation initialization in particular drags). Spawn rate exceeded exit rate; subprocesses piled up. After 60 seconds the user had ~50 ScreenCap helpers in the dock and the process table was filling.

**Lesson:** any periodic Process spawn needs *both* a known-bounded execution time *and* an explicit guard that doesn't start a new spawn while the previous one is still running. "Just throw it on a timer" is a fork-bomb generator.

## Prevention checklist

For every `Foundation.Process` use site on macOS, verify:

- [ ] **stdout drained** even if you don't care about the lines. Attach a readabilityHandler that reads `availableData`. Discard the bytes if you must.
- [ ] **stderr drained** with the same care.
- [ ] **terminationHandler drains pipes to EOF** before flushing buffers and signaling completion. Use `readToEnd()` on the pipe handle after nilling the readabilityHandler.
- [ ] **No sub-binary tricks for TCC.** If you need fresh TCC state, relaunch the entire .app via LaunchServices.
- [ ] **Timer-driven spawns have an in-flight guard** (`@Published var inFlight: Bool`), rate limiting, or a fixed concurrency cap.
- [ ] **Timeout has a SIGTERM → SIGKILL escalation** with a bounded grace period. `process.terminate()` followed by an unbounded `waitUntilExit()` lets a stuck child hang the host forever.

## Why the fixes work

1. **Drain stdout always.** macOS pipes have a single reader semantic — bytes don't leave the buffer until something reads. The Process API doesn't auto-drain; that's our job.
2. **readToEnd in terminationHandler.** Once the child exits and closes its FD, the kernel pipe still holds the unread bytes. They're addressable until we drop the FD. `readToEnd()` collects them in one synchronous call — no race with the readabilityHandler since we've nilled it first.
3. **LaunchServices preserves bundle identity.** `NSWorkspace.openApplication` (and `/usr/bin/open`) go through LaunchServices, which knows about bundles and registers the launched process under the bundle id. Direct `exec` skips this.
4. **In-flight guard prevents pile-up.** A `@Published` Bool checked at the start of the spawn function is enough; SwiftUI bindings disable the trigger button automatically.

## Related

- [TCC per-process cache + Quit & Relaunch](../runtime-errors/macos-tcc-per-process-cache-quit-and-relaunch.md) — the related TCC behavior that motivated the failed sub-binary attempt.
- [Ad-hoc dev signing TCC rebuild treadmill](../build-errors/macos-ad-hoc-signing-tcc-rebuild-treadmill.md) — adjacent dev-time pain.
- `docs/architecture/swiftui-shell.md` — CLIClient + PermissionController architecture.
- `macos/README.md` — dev launch instructions.
