---
title: "SwiftUI WindowGroup vs Window: pick Window for a singleton main window"
date: 2026-05-18
category: ui-bugs
module: macos-app-shell
problem_type: ui_bug
component: tooling
symptoms:
  - "Clicking menu bar Open Screencap creates a second main window instead of focusing the existing one"
  - "Dock-click / app relaunch (applicationShouldHandleReopen) spawns a duplicate main window after the original was closed"
  - "From a zero-window state (last main window closed, app in .accessory mode), every openWindow(id:) call produces a fresh window — even after the prior call already created one"
  - "The first-run permissions sheet re-presents on the duplicate window, so the user sees two main windows each with their own sheet"
root_cause: wrong_api
resolution_type: code_fix
severity: medium
related_components:
  - development_workflow
tags:
  - macos
  - swiftui
  - window-management
  - windowgroup
  - single-window
  - menu-bar
  - scr-55
  - app-lifecycle
---

# SwiftUI WindowGroup vs Window: pick Window for a singleton main window

## Problem

Closing and reopening Screencap's main window produced duplicate main windows from both the menu bar's "Open Screencap" button and the Dock/relaunch reopen path (`AppDelegate.applicationShouldHandleReopen`). The plan (SCR-55) assumed switching from `NSApp.windows` iteration to SwiftUI's `openWindow(id:)` action on a `WindowGroup` scene would dedup — it does not. The actual fix is a one-line scene-type change from `WindowGroup` to `Window` (macOS 13+ singleton scene).

## Symptoms

- Menu bar "Open Screencap" → 2 main windows instead of 1, even with no main window previously visible.
- Dock-click / relaunch reopen → same duplication path with `applicationShouldHandleReopen(_, hasVisibleWindows: false)`.
- With the main window already visible, clicking "Open Screencap" added a second window rather than focusing the existing one.
- Both duplicate windows independently presented the first-run permissions sheet, since each window evaluates its own `.sheet(isPresented:)` state.

## What Didn't Work

- **Attempt 1 — naive switch to `openWindow(id:)` on a `WindowGroup`** (commit `a8a4ef2e`). Gave the existing `WindowGroup` an explicit id (`MainWindowID = "main"`), added `@Environment(\.openWindow)` to `MenuBarMenu`, and introduced a `WindowOpener` bridge so `AppDelegate.applicationShouldHandleReopen` could call the same SwiftUI action via a stored closure (`OpenWindowBridge` view inside the scene body captured `openWindow` into `WindowOpener.shared.openMain`). This fixed the *close-then-reopen* path the plan was scoped for, but every `openWindow(id:)` call on a `WindowGroup` instantiated a fresh window — so the with-window-visible case duplicated.
- **Attempt 2 — fast-path walk over `NSApp.windows` before `openWindow`** (commit `b9e2513e`). Mirrored the AppDelegate filter (titled, non-panel, content view, `sheetParent == nil`) inside `MenuBarMenu.openMainWindow()`. Fixed the already-visible case (we focused the existing window and skipped the duplicate-creating call), but the zero-window case still produced two windows. Speculation about an AppKit auto-restoration race turned out to be wrong — the real issue was that the workaround was treating a symptom, not the cause.
- **Root cause both attempts missed**: `WindowGroup` is a multi-window scene by contract — `openWindow(id:)` against it *always* creates a new window of the group, regardless of how the caller guards the call site. The SCR-55 plan's "Scope Boundaries" section explicitly excluded "a `Window` (vs `WindowGroup`) scene rewrite," which turned out to be the actual fix.

## Solution

Final fix (commit `2dcf8e67`) — a one-line scene-type change in [macos/Screencap/ScreencapApp.swift](macos/Screencap/ScreencapApp.swift):

```swift
// Before — multi-window scene, every openWindow(id:) call creates a new window
WindowGroup("Screencap", id: MainWindowID) { ... }

// After — singleton scene, openWindow(id:) focuses or creates the lone instance
Window("Screencap", id: MainWindowID) { ... }
```

The defensive scaffolding from attempts 1 and 2 is intentionally kept as belt-and-suspenders against a future contributor flipping the scene back to `WindowGroup`:

- [`WindowOpener`](macos/Screencap/State/WindowOpener.swift) — `@MainActor` singleton holding `var openMain: (() -> Void)?`. AppDelegate-callable closure surface.
- `OpenWindowBridge` (private view in [`ScreencapApp.swift`](macos/Screencap/ScreencapApp.swift)) — zero-frame `Color.clear` view inside the scene body that captures `@Environment(\.openWindow)` into `WindowOpener.shared.openMain` on `.onAppear`. This runs on every window materialization.
- [`MenuBarMenu.openMainWindow()`](macos/Screencap/Views/MenuBarMenu.swift) — fast-path walk over `NSApp.windows` (filters `contentViewController != nil`, not `NSPanel`, `.styleMask.contains(.titled)`, `sheetParent == nil`) before calling `openWindow(id: MainWindowID)`.
- [`AppDelegate.applicationShouldHandleReopen`](macos/Screencap/AppDelegate.swift) — same filter walk, falls back to `WindowOpener.shared.openMain?()`.

With `Window` as the scene type these guards rarely fire (the scene dedups natively), but they make a regression safer to spot.

## Why This Works

Per Apple's SwiftUI docs for [`OpenWindowAction`](https://developer.apple.com/documentation/swiftui/openwindowaction):

> If the action's identifier corresponds to a `WindowGroup` scene, this presents a **new window** of that group.
>
> If the action's identifier corresponds to a `Window` scene, this presents the window if it isn't already opened. If the window is already opened, it is brought to the front.

- `WindowGroup` is **multi-window by design**. Each `openWindow(id:)` call instantiates a new window. There is no built-in dedup, even with a declared id — the id is for value-keyed addressing across multiple instances, not for forcing a singleton.
- `Window` (introduced macOS 13.0) is a **singleton scene**. `openWindow(id:)` focuses the existing window if it's open, or materializes the single instance if it isn't.

The scope-bounded API swap (`NSApp.windows` iteration → `openWindow(id:)`) didn't change the underlying multi-window contract. Only the scene type does.

## Prevention

- **SwiftUI scene-type heuristic**: choose `Window` when the app has exactly one of a thing (main window, settings, inspector). Use `WindowGroup` only when true multi-window or multi-document behavior is intended. "Single main window" is `Window`, full stop. macOS 13+ deployment targets get `Window`; pre-13 codebases have to live with `WindowGroup` and explicit dedup logic.
- **Re-validate plan scope boundaries when the underlying framework assumption is the bug**. The SCR-55 plan excluded the `Window`-vs-`WindowGroup` rewrite based on the (wrong) belief that `openWindow(id:)` dedups on `WindowGroup`. When QA fails twice on the same root behavior, revisit excluded options before adding more guard code.
- **Testing surface reminder**: `MenuBarExtra` button actions and `OpenWindowAction` invocations are not unit-testable in this codebase's XCTest harness. The only seam is [`WindowOpenerTests`](macos/ScreencapTests/WindowOpenerTests.swift) (closure registration / nil-safe no-op / reassignment). Everything else is manual QA — keep the U2/U3 manual scenarios in [the SCR-55 plan](docs/plans/2026-05-18-001-fix-menu-bar-open-screencap-window-recreate-plan.md) as living checklists for any future window-lifecycle change.
- **Keep the WHY comment on the scene declaration**. `ScreencapApp.swift` now carries a short comment above the `Window(...)` call explaining why it isn't `WindowGroup`; preserve that comment in any future refactor so the next contributor doesn't undo the fix.

## Related Issues

- [SCR-55](https://linear.app/zk-email/issue/SCR-55/make-menu-bar-open-screencap-recreate-the-main-window-after-close) — origin ticket.
- [PR #181](https://github.com/proteus-computer-use/screencap/pull/181) — three-commit shipping history (`a8a4ef2e`, `b9e2513e`, `2dcf8e67`) walking through the two failed attempts and the final fix.
- See also: [`integration-issues/macos-foundation-process-pipe-pitfalls.md`](docs/solutions/integration-issues/macos-foundation-process-pipe-pitfalls.md) — sibling SwiftUI/AppKit gotcha doc for the same `macos-app-shell` module.
