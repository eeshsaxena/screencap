---
title: "SwiftUI: hiding a NavigationSplitView's window toolbar occludes the traffic-light buttons"
date: 2026-07-06
category: ui-bugs
module: macos-app-shell
problem_type: ui_bug
component: tooling
symptoms:
  - "The macOS main window shows no close / minimize / zoom (traffic-light) buttons in its top-left corner"
  - "The buttons report hidden=false, alpha=1.0, enabled=true — they are present and functional, just not visible"
  - "The window can still be closed via ⌘W and the app menu, so it is occlusion, not a disabled/removed control"
root_cause: wrong_api
resolution_type: code_fix
severity: high
tags:
  - macos
  - swiftui
  - navigationsplitview
  - hidden-title-bar
  - window-chrome
  - traffic-lights
  - window-management
---

# SwiftUI: hiding a NavigationSplitView's window toolbar occludes the traffic-light buttons

## Problem

Screencap's main window showed no close / minimize / zoom buttons. The buttons were present, unhidden, and fully functional the whole time — they were **visually occluded** by the `NavigationSplitView` sidebar's hosting view, which had expanded over the top edge of the window and painted its opaque background on top of them.

## Symptoms

- No traffic-light buttons visible in the window's top-left corner.
- `window.standardWindowButton(.closeButton/.miniaturizeButton/.zoomButton)` all return non-nil with `isHidden == false`, `alphaValue == 1.0`, `isEnabled == true` — so nothing is hidden or removed.
- The window is still closable with ⌘W and via the app/menu — confirming the buttons work but can't be clicked because something is drawn over them.

## What Didn't Work

- **Blaming `.windowStyle(.hiddenTitleBar)` or `.windowResizability(.contentSize)` alone.** A minimal repro with those exact modifiers over plain content keeps all three buttons visible and on top. The modifiers are not the culprit by themselves.
- **Raising the titlebar container in AppKit after launch** (`parent.addSubview(titlebarContainer, positioned: .above, relativeTo: nil)`). The SwiftUI split view re-establishes its own z-order, so the buttons stayed occluded. Fragile and ineffective.

## Solution

The window content was a `NavigationSplitView` whose sidebar applied `.toolbar(.hidden, for: .windowToolbar)`. Under `.windowStyle(.hiddenTitleBar)` (transparent titlebar + `fullSizeContentView`, so content fills to the window's top edge), hiding the window toolbar collapses the reserved titlebar/toolbar strip. The split view's sidebar `NSHostingView` then expands to the window's top edge and is z-ordered **above** the `NSTitlebarContainerView` traffic-light widgets, covering them.

Because the app routes purely through a `route` state value — there is **no** `NavigationLink` / `NavigationStack` / `navigationDestination` anywhere — the `NavigationSplitView` contributed no behavior, only the native split chrome that caused the occlusion. The fix is to replace it with a plain two-column `HStack`.

Before (`MainWindow.swift` `shellContent` + `sidebar`):

```swift
NavigationSplitView {
    sidebar            // ShellSidebarView(...)
        .navigationSplitViewColumnWidth(248)
        .navigationTitle("Screencap")
        .toolbar(.hidden, for: .windowToolbar)   // ← collapses the strip → sidebar covers the buttons
} detail: {
    detailColumn
}
```

After:

```swift
HStack(spacing: 0) {
    sidebar            // ShellSidebarView(...)
        .frame(width: 248)          // width: and maxHeight: are DISTINCT frame overloads —
        .frame(maxHeight: .infinity) // they cannot be combined into one .frame(...) call
    detailColumn
        .frame(maxWidth: .infinity, maxHeight: .infinity)
}
```

The custom `ShellSidebarView` already reserves a top-left slot for the real traffic lights and fills the full window height, so the `.hiddenTitleBar` buttons now overlay that slot exactly as intended.

### Reusable diagnosis: hit-test for occlusion, not just `isHidden`

`isHidden` / `alphaValue` / `isEnabled` cannot detect occlusion. Hit-test each button's center against the window's theme frame in a minimal repro's `applicationDidFinishLaunching` (after a short delay):

```swift
let themeFrame = window.contentView?.superview            // NSThemeFrame
let btn = window.standardWindowButton(.closeButton)!
let center = btn.convert(NSPoint(x: btn.bounds.midX, y: btn.bounds.midY), to: themeFrame)
let top = themeFrame?.hitTest(center)
// top is _NSThemeCloseWidget  → visible (on top)
// top is NSHostingView<...ColumnView...> → occluded (covered by SwiftUI content)
```

Empirically: `NavigationSplitView` + hidden window toolbar → occluded (regardless of title-bar style); plain `HStack` → not occluded; keeping the toolbar visible → not occluded.

## Why This Works

The occlusion is a view-hierarchy z-order problem, not a hidden/disabled button. Removing the `NavigationSplitView` removes the sidebar `NSHostingView` that AppKit was placing above the titlebar container once the toolbar strip collapsed. A plain `HStack` is an ordinary content view that stays below the `NSTitlebarContainerView`, so the standard window buttons render on top — which is the whole point of `.hiddenTitleBar` (float the real traffic lights over full-bleed content).

## Prevention

- **Do not hide the window toolbar on a `NavigationSplitView` while relying on `.hiddenTitleBar` to reveal the traffic lights** — the sidebar hosting view will cover them. The toolbar strip is what reserves the clear space the buttons live in.
- **If the sidebar is a fully custom fixed-width column (not a native `List` sidebar) and routing does not use `NavigationLink`/`NavigationStack`, prefer a plain `HStack`.** `NavigationSplitView` then adds only native chrome you have to fight.
- On macOS 14+, `.toolbar(removing: .sidebarToggle)` can suppress just the sidebar toggle while keeping the toolbar strip (and buttons) — but Screencap targets macOS 13, so that path is unavailable here.
- **Unit tests can't catch this** — it's a runtime AppKit z-order/occlusion issue, invisible to logic-only XCTest (734 green tests shipped the regression). Guard it with a manual visual check on window-chrome changes, or a UI test that hit-tests the buttons.
- The `.hiddenTitleBar` and the toolbar-hide were introduced together in commit `78cf55fb` and had been in direct conflict ever since — a reminder that "reveal the real traffic lights" and "hide the toolbar" are contradictory on a split view.

## Related

- [SwiftUI WindowGroup vs Window: pick Window for a singleton main window](swiftui-windowgroup-vs-window-singleton-scene-2026-05-18.md) — same `MainWindow.swift` / macOS window-scene area; another case where an assumption about SwiftUI window-scene behavior didn't hold and the fix was a scene/container change.
- Fixed in PR #335.
