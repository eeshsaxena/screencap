---
title: "fix: Make menu bar Open ScreenCap recreate the main window after close (SCR-55)"
type: fix
status: completed
date: 2026-05-18
origin: https://linear.app/zk-email/issue/SCR-55/make-menu-bar-open-screencap-recreate-the-main-window-after-close
---

# fix: Make menu bar Open ScreenCap recreate the main window after close (SCR-55)

## Summary

Switch the menu bar's `Open ScreenCap` action from iterating `NSApp.windows` to invoking SwiftUI's `openWindow(id:)`, so the action recreates the main window after `WindowGroup` has destroyed it. Add a tiny SwiftUI-to-AppKit bridge so `AppDelegate.applicationShouldHandleReopen` (Dock/relaunch path) shares the same recreate behavior.

---

## Problem Frame

When the user closes the last main window, `AppDelegate` flips the activation policy to `.accessory` and SwiftUI tears down the `WindowGroup` window. The menu bar item stays alive, but `MenuBarMenu.openMainWindow()` only scans `NSApp.windows` and calls `makeKeyAndOrderFront` on whatever it finds — so once the window is destroyed, the `Open ScreenCap` button silently no-ops (or surfaces a stray panel via the `NSApp.windows.first` fallback). Same hazard applies to `AppDelegate.applicationShouldHandleReopen`, which also iterates `NSApp.windows` without a recreate fallback.

---

## Requirements

- R1. Choosing `Open ScreenCap` from the menu bar after the last main window is closed shows the main `WindowGroup` window (Linear AC #1).
- R2. Reopen from Dock / app relaunch while the app is in `.accessory` mode continues to show the main window (Linear AC #2).
- R3. Behavior is covered by either an automated window-management test or a manual QA note (Linear AC #3).
- R4. No regression to the existing close-to-accessory flow in `AppDelegate.applicationDidFinishLaunching` (the `willCloseNotification` observer must still flip to `.accessory` only when no real titled windows remain).

---

## Scope Boundaries

- Not refactoring `AppDelegate`'s close-detection observer or its sheet/panel filtering — that logic works correctly today.
- Not adding URL-scheme deep-linking, multi-window support, or a `Window` (vs `WindowGroup`) scene rewrite.
- Not touching `RecorderController`, permission flow, or daemon transport — separate from this surface (see SCR-54).
- Not changing the `MenuBarExtra` icon, label, or keyboard shortcuts.

---

## Context & Research

### Relevant Code and Patterns

- `macos/ScreenCap/ScreenCapApp.swift` — defines `WindowGroup("ScreenCap")` (no id) and the `MenuBarExtra`.
- `macos/ScreenCap/AppDelegate.swift` — `applicationDidFinishLaunching` installs the close observer; `applicationShouldHandleReopen` handles Dock/relaunch and iterates `NSApp.windows`.
- `macos/ScreenCap/Views/MenuBarMenu.swift:42-53` — current `openMainWindow()` iterates `NSApp.windows` by title and falls back to `NSApp.windows.first`.
- `macos/ScreenCap/Views/MainWindow.swift` — top-level `WindowGroup` content; an in-body helper view is a natural place to capture the `openWindow` environment value.
- No existing test file for menu bar / window plumbing — `macos/ScreenCapTests/` covers `RecorderController`, `PermissionController`, `DaemonClient`, `DaemonInstallController`, `QuitProgressCountdown`. A new small test file is appropriate.

### Institutional Learnings

- `docs/solutions/` has no prior entries for SwiftUI window lifecycle or `openWindow`; this is the first time the codebase touches that API. New learnings (if any surface during implementation) should be filed under `docs/solutions/integration-issues/` or a new `swiftui-window-lifecycle` category.

### External References

- Deployment target is macOS 13.0 (`macos/ScreenCap.xcodeproj/project.pbxproj`), so `WindowGroup(_:id:)` and `@Environment(\.openWindow)` (both macOS 13+) are available without availability guards.

---

## Key Technical Decisions

- **Use SwiftUI's `openWindow(id:)` rather than AppKit `NSApp.windows` iteration.** The ticket prescribes this direction; it is the canonical SwiftUI 13+ mechanism to materialize a `WindowGroup` window that has been torn down.
- **Bridge SwiftUI's `openWindow` action to AppDelegate via a small shared holder.** `AppDelegate.applicationShouldHandleReopen` runs in an AppKit context with no access to `@Environment(\.openWindow)`. A minimal `WindowOpener` `ObservableObject` (or `@MainActor` final class) with a `var openMain: (() -> Void)?` closure, populated by a hidden helper view inside the `WindowGroup` body, gives the AppDelegate a reliable call site without depending on undocumented AppKit default-reopen behavior. Alternative considered: relying on AppKit to recreate the `WindowGroup` window when `applicationShouldHandleReopen` returns `true` — rejected as too dependent on framework specifics, especially with `CommandGroup(replacing: .newItem) {}` explicitly removing the standard New Window pathway.
- **Keep the activation-policy flip (`.regular`) and `NSApp.activate` calls at both entry points.** Required to bring the app back to the foreground when it was in `.accessory`. The `openWindow` call is additive, not a replacement, for the activation-policy fix.
- **Constant for the window id.** Define `MainWindowID = "main"` as a single source of truth (in `ScreenCapApp.swift` or a small constants file) so the producer (`WindowGroup`) and consumers (`MenuBarMenu`, `WindowOpener` bridge) cannot drift.

---

## Open Questions

### Resolved During Planning

- **Should AppDelegate use `openWindow` instead of iterating `NSApp.windows`?** Yes — via the `WindowOpener` bridge described above. Iterating windows after `WindowGroup` teardown is the bug we are fixing; the Dock/relaunch path has the same root cause.
- **Do we need a custom URL scheme or `Window` scene rewrite?** No — `WindowGroup(id:)` + `openWindow(id:)` is sufficient and minimally invasive.

### Deferred to Implementation

- **Exact shape of the `WindowOpener` holder** (shared singleton vs. injected `@StateObject`). The plan recommends a `@MainActor` shared singleton because it must be reachable from `AppDelegate` (which is created via `@NSApplicationDelegateAdaptor` and is not in the SwiftUI environment chain in a clean way). Implementer may pick the cleanest form.
- **Whether to delete `NSApp.windows.first` dead-end fallback in MenuBarMenu.** Likely yes once `openWindow` is in place, but worth a quick check that no transient panel scenario relied on it.

---

## Implementation Units

### U1. Give the main WindowGroup an explicit id and define a shared id constant

**Goal:** Make the main window addressable via `openWindow(id:)`.

**Requirements:** R1, R2

**Dependencies:** None

**Files:**
- Modify: `macos/ScreenCap/ScreenCapApp.swift`

**Approach:**
- Add a top-level constant (or `enum` namespace) `MainWindowID = "main"` near `ScreenCapApp` so producers and consumers share one symbol.
- Change `WindowGroup("ScreenCap")` to `WindowGroup("ScreenCap", id: MainWindowID)`.
- No other behavior change in this unit.

**Patterns to follow:**
- The file already uses top-level Swift declarations alongside `@main struct`; a small `enum` or `let` constant fits naturally.

**Test scenarios:**
- Test expectation: none — pure scene-configuration change with no branching logic; behavior is exercised via U2 and U3 manual QA.

**Verification:**
- Project builds (`xcodebuild` / Xcode build) with no new warnings.
- `WindowGroup` still renders `MainWindow` on first launch (manual smoke).

---

### U2. Wire menu bar `Open ScreenCap` to `openWindow(id:)`

**Goal:** Replace the `NSApp.windows`-iteration logic in `MenuBarMenu.openMainWindow()` with SwiftUI's environment-provided `openWindow` action so the window is recreated when needed.

**Requirements:** R1

**Dependencies:** U1

**Files:**
- Modify: `macos/ScreenCap/Views/MenuBarMenu.swift`

**Approach:**
- Add `@Environment(\.openWindow) private var openWindow` to `MenuBarMenu`.
- Rewrite `openMainWindow()` to:
  1. Set activation policy to `.regular` (unchanged — needed when coming from `.accessory`).
  2. `NSApp.activate(ignoringOtherApps: true)` (unchanged).
  3. Call `openWindow(id: MainWindowID)` — SwiftUI either focuses the existing window or creates one if `WindowGroup` torn it down.
- Remove the `for window in NSApp.windows where window.title == "ScreenCap"` loop and the `NSApp.windows.first` fallback — both become dead code with `openWindow` in place.

**Patterns to follow:**
- `MenuBarMenu` already uses `@EnvironmentObject` for `RecorderController`; `@Environment(\.openWindow)` is the same general pattern.

**Test scenarios:**
- Test expectation: none automated — `MenuBarExtra` content is a SwiftUI view tree without a unit-test seam in this codebase, and `OpenWindowAction` cannot be invoked in a pure XCTest harness. Covered by manual QA below and the bridge test in U3.
- **Manual QA (covers AE#1):** Launch app → close the main window (app flips to `.accessory`) → click menu bar icon → click `Open ScreenCap` → main window appears and becomes key.
- **Manual QA (regression):** With the main window already visible, click `Open ScreenCap` → window comes to front without duplication.

**Verification:**
- The two manual QA scenarios above both pass.
- No new compiler warnings; `NSApp.windows` no longer referenced in `MenuBarMenu.swift`.

---

### U3. Add SwiftUI→AppKit bridge so AppDelegate reopen path also recreates the window

**Goal:** Make `AppDelegate.applicationShouldHandleReopen` recreate the main `WindowGroup` window when none exist, matching the menu bar behavior so the Dock/relaunch path still works after window teardown (AC #2).

**Requirements:** R2, R3, R4

**Dependencies:** U1, U2

**Files:**
- Create: `macos/ScreenCap/State/WindowOpener.swift`
- Create: `macos/ScreenCapTests/WindowOpenerTests.swift`
- Modify: `macos/ScreenCap/ScreenCapApp.swift` — add a tiny helper subview inside the `WindowGroup` body that captures `openWindow` into `WindowOpener.shared`.
- Modify: `macos/ScreenCap/AppDelegate.swift`

**Approach:**
- Introduce `@MainActor final class WindowOpener: ObservableObject` exposing `static let shared = WindowOpener()` and `var openMain: (() -> Void)?` (settable). Keeping this an `ObservableObject` is forward-compatible if any view ever wants to observe state, but the load-bearing surface is the closure.
- Inside the main `WindowGroup` body, add a small zero-frame helper view (e.g., `OpenWindowBridge()`) that reads `@Environment(\.openWindow)` and, in `.onAppear` / `.task`, sets `WindowOpener.shared.openMain = { openWindow(id: MainWindowID) }`. Capturing the action inside a closure is necessary because `OpenWindowAction` is `@MainActor`-bound and only valid inside SwiftUI's environment.
- Rewrite `AppDelegate.applicationShouldHandleReopen` so that when `!hasVisibleWindows`:
  1. Flip policy to `.regular` and activate (unchanged).
  2. If `NSApp.windows` contains a visible main window with `contentViewController`, bring it forward (preserves current fast path for the case where a stray titled window survived).
  3. Otherwise, call `WindowOpener.shared.openMain?()` to ask SwiftUI to materialize a fresh window.
  4. Return `true`.
- Leave the close-observer in `applicationDidFinishLaunching` untouched (R4).

**Execution note:** Start by writing the `WindowOpener` logic test (closure registration / invocation) before touching SwiftUI wiring — gives a tight TDD loop on the only piece that has unit-test seams.

**Patterns to follow:**
- `RecorderController` / `PermissionController` use `@MainActor final class`, dependency injection patterns, and have `#if DEBUG` test seams — mirror that style for `WindowOpener` even though the production surface is tiny.
- `DaemonClientTests` is a small focused logic test file — `WindowOpenerTests` should follow the same lightweight shape.

**Test scenarios:**
- **Happy path (XCTest):** `WindowOpener.shared.openMain` starts as `nil`; assigning a closure and invoking via `openMain?()` runs the closure exactly once. Reset to `nil` in `tearDown` to avoid cross-test bleed.
- **Edge case (XCTest):** Calling `openMain?()` when no closure is registered is a safe no-op (does not crash). This guards the AppDelegate path against ordering issues where reopen fires before the bridge has installed itself (e.g., very early app launch).
- **Manual QA (covers AE#2):** Launch app → close main window → app is in `.accessory` mode → quit and relaunch the `.app` bundle from Finder → `applicationShouldHandleReopen` fires → main window appears.
- **Manual QA (regression for R4):** Launch app → present any `NSAlert` (e.g., a permissions prompt) → dismiss the alert → main window remains, policy stays `.regular` (the close observer must not flip to `.accessory` on alert/sheet dismissal).
- **Manual QA (regression):** Launch app → click Dock icon while main window is visible → no duplicate window appears, existing window stays key.

**Verification:**
- `WindowOpenerTests` passes locally and in CI.
- All three manual QA scenarios above pass.
- `AppDelegate.applicationShouldHandleReopen` no longer silently no-ops when the `WindowGroup` window has been destroyed.

---

## System-Wide Impact

- **Interaction graph:** Two entry points (menu bar `Open ScreenCap`, AppDelegate reopen) converge on the same `openWindow(id: MainWindowID)` action via the new `WindowOpener` bridge. Activation policy flip remains at both call sites.
- **Error propagation:** No new error surface. `openWindow` either focuses an existing window or instantiates one; failure modes are SwiftUI-internal.
- **State lifecycle risks:** `WindowOpener.shared.openMain` lifetime is tied to whether any `WindowGroup` window has been materialized at least once. If the first-ever reopen fires before any window has appeared (cold launch into accessory mode — not currently possible since `applicationDidFinishLaunching` sets `.regular`), the closure is `nil`. Guarded by the optional chaining in U3's edge-case test.
- **API surface parity:** Both surfaces (menu bar, AppDelegate) now recreate the window. The dock-click-while-window-exists path is unchanged (handled by AppKit + the early-return in U3).
- **Integration coverage:** SwiftUI `openWindow` behavior itself is not unit-testable in this harness — covered by the manual QA scenarios in U2 and U3.
- **Unchanged invariants:** `AppDelegate.applicationDidFinishLaunching` close observer (with its panel/sheet filtering and `DispatchQueue.main.async` deferral); `applicationShouldTerminateAfterLastWindowClosed` returning `false`; `applicationShouldTerminate` recording-confirmation flow; `MenuBarExtra` icon binding to recorder state.

---

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| `WindowOpener.shared.openMain` is `nil` at the moment AppDelegate reopen fires (e.g., a very early relaunch path before SwiftUI has rendered any view). | Edge-case test in U3 asserts safe no-op. In practice the bridge subview installs in `.onAppear`/`.task` on the first main window, before any user-driven close-and-reopen sequence. |
| Hidden helper view inside `MainWindow` adds a maintenance footgun if a future contributor removes it. | Add a one-line `// Captures openWindow for AppDelegate; see WindowOpener / SCR-55.` comment at the helper-view declaration. (This is a justified "WHY non-obvious" comment per CLAUDE.md.) |
| `openWindow(id:)` opens a *new* window rather than focusing an existing one in some macOS versions. | macOS 13+ documented behavior is to focus an existing scene instance for `WindowGroup` with a single declared id. Manual QA regression scenario in U2 catches duplication if it happens. |
| Removing `NSApp.windows`-iteration fallback in `MenuBarMenu` masks an unrelated transient-panel issue. | Manual QA covers the with-window-visible and after-close cases; if a regression surfaces, restore the early-return path that focuses an existing window before calling `openWindow`. |

---

## Documentation / Operational Notes

- Optional: file a `docs/solutions/integration-issues/swiftui-windowgroup-recreate-on-menubar-2026-05-18.md` learning capturing the `WindowGroup(id:)` + `@Environment(\.openWindow)` + AppDelegate bridge pattern. This is the first place this codebase touches SwiftUI window lifecycle and a future contributor will hit the same question.
- No rollout, migration, or monitoring concerns — pure client-side bug fix.
- Update Linear ticket SCR-55 status when the PR lands; branch name `rutefig/scr-55-make-menu-bar-open-screencap-recreate-the-main-window-after` is already suggested in Linear.

---

## Sources & References

- **Origin ticket:** [Linear SCR-55](https://linear.app/zk-email/issue/SCR-55/make-menu-bar-open-screencap-recreate-the-main-window-after-close)
- **Parent ticket:** SCR-13 (MacOS app shell)
- Related code:
  - `macos/ScreenCap/ScreenCapApp.swift`
  - `macos/ScreenCap/AppDelegate.swift`
  - `macos/ScreenCap/Views/MenuBarMenu.swift`
- Apple docs: `WindowGroup(_:id:content:)`, `EnvironmentValues.openWindow`, `OpenWindowAction` (macOS 13+).
