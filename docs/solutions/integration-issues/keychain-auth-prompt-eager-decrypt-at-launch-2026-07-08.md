---
title: "macOS Keychain 'screencap-auth' prompt fires at app launch — never eagerly decrypt a stored credential on the launch path"
date: 2026-07-08
category: integration-issues
module: macos-app-shell
problem_type: integration_issue
component: authentication
platform: macos
severity: medium
symptoms:
  - "A macOS system dialog pops the instant ScreenCap opens: \"ScreenCap wants to use your confidential information stored in 'screencap-auth' in your keychain.\""
  - "The prompt appears before the user touches anything cloud-related (no sign-in, no upload)"
  - "It recurs on every launch even after clicking \"Always Allow\" — common on a dev machine after each rebuild"
root_cause: "Eager `whoami` probe at app launch decrypts the Keychain refresh token, and the decrypt hits macOS's per-binary ACL authorization prompt whenever the reading binary's code identity differs from the one that ran `login`"
resolution_type: code_fix
tags:
  - keychain
  - keyring
  - macos
  - swiftui
  - menubarextra
  - authentication
  - app-launch
  - code-signing
related_components:
  - screencap.auth
  - CloudAuthController
---

# macOS Keychain 'screencap-auth' prompt at app launch

## Problem

Opening the ScreenCap macOS app popped a system Keychain authorization dialog
(*"ScreenCap wants to use your confidential information stored in
'screencap-auth'"*) the instant the window appeared — before the user did
anything cloud-related. Clicking "Always Allow" did not make it stick; it
returned on the next launch (especially after a dev rebuild).

## Symptoms

- The macOS Keychain prompt for the `screencap-auth` generic-password item
  appears at launch, unprompted by any user action.
- It only happens for users who have signed in at least once (a missing item
  reads silently as `errSecItemNotFound` — no dialog).
- It recurs every launch; "Always Allow" doesn't help across rebuilds.

## What Didn't Work / What It Is NOT

- It is **not** a code-signing/notarization bug in the build itself.
- It is **not** fixed by re-granting or toggling "Always Allow" — the grant is
  pinned to a code identity that keeps changing.
- Suppressing the prompt by making the read silent (e.g. a shared Keychain
  access group) would **not** address the reported symptom on its own: the app
  would still decrypt at launch: you'd only trade a visible prompt for an
  invisible one and leave the "reads at launch before any cloud use" behavior
  in place. (That storage-layer change is still worthwhile long-term — tracked
  as SCR-241 — but it is orthogonal to *when* the read happens.)

## Root Cause

Two facts combined:

1. **The app decrypted the Keychain at launch.** A launch-time SwiftUI scene
   `.task` unconditionally ran `await auth.refresh()` → `screencap whoami
   --json` → Python `auth.whoami()` → `keyring.get_password("screencap-auth",
   "default")`. `keyring.get_password` on macOS reads the **secret data**, which
   forces a Keychain *decrypt* — and decrypt is what triggers the ACL check.

2. **The reading binary's code identity didn't match the item's ACL.**
   `keyring`'s macOS backend creates the generic-password item with a
   trusted-application ACL bound to the **exact code identity of the binary that
   ran `login`**. This machine had several distinct `screencap` identities — the
   pyenv terminal CLI (`~/.pyenv/shims/screencap`), the app-bundled CLI, and
   Xcode Debug builds that are **re-signed with a new cdhash on every build**. A
   launch decrypt from a *different* identity than the one that stored the token
   can't silently match the ACL, so macOS prompts. This is the same
   signing-churn root cause as SCR-157 (dev-build TCC identity churn), applied
   to the Keychain ACL instead of TCC.

Predominantly a **dev-environment** artifact: a stable Developer-ID release read
*through the app it signed in with* keys the ACL off the app's designated
requirement and generally survives updates. It bites when identities differ
(dev rebuilds, or "signed in via the terminal CLI, then opened the app").

## Solution

**Never eagerly decrypt Keychain-backed state on the launch path. Resolve it
lazily, only when a surface that needs it actually appears.**

Removed the launch `.task` probe and added a coalesced lazy resolver that cloud
surfaces call on-appear (menu-bar account section, Upload gate, cloud
onboarding). See PR #350; key file
`macos/ScreenCap/Controllers/CloudAuthController.swift`.

```swift
// CloudAuthController (@MainActor). Coalesced two ways:
//  - no-ops once `status` resolves away from `.unknown` (decrypt at most once/session)
//  - concurrent callers share one in-flight Task (two surfaces can't double-prompt)
private var pendingLazyRefresh: Task<Void, Never>?

func refreshIfNeeded() async {
    guard status == .unknown else { return }
    if let pendingLazyRefresh {
        await pendingLazyRefresh.value
        return
    }
    let task = Task { await self.refresh() }   // refresh() catches internally, never throws
    pendingLazyRefresh = task
    await task.value
    pendingLazyRefresh = nil
}
```

Call sites decrypt only on genuine cloud engagement:

```swift
// MenuBarMenu — resolves on first menu OPEN, not at app launch (see reusable fact #2)
accountSection.onAppear { Task { await auth.refreshIfNeeded() } }

// ReviewWindow.attemptUpload — refresh BEFORE the gate, so a signed-in user whose
// status is still .unknown isn't wrongly shown the sign-in sheet
Task {
    await auth.refreshIfNeeded()
    if auth.isSignedIn { model.startUpload() } else { showSignInSheet = true }
}
```

## Why This Works

The Keychain is never decrypted just because the app opened — only when the user
engages a cloud surface, which is exactly where a credential prompt is
contextually appropriate. Local recording never depended on sign-in, so nothing
at launch regresses. The coalescing guarantees the decrypt (and any prompt)
happens at most once per session even if several surfaces appear at once.

Verified via an instrumented launch (a CLI shim logging every invocation): the
only call at launch was `list --json`; `whoami` never fired.

## Two Reusable Facts

**1. `keyring.get_password` DECRYPTS the item.** Any read of a stored credential
(a `whoami` probe, a "am I signed in?" check) triggers the macOS ACL
authorization prompt when the reading binary's code identity isn't silently
authorized. So a read is not free — treat "read the stored token" as "may prompt
the user" and keep it off eager/launch/hot paths. (`src/screencap/auth.py`
already documented the daemon-spawned-engine variant of this — *"the
daemon-spawned engine subprocess, whose Keychain ACL identity differs from the
interactive login binary"* — and works around it with an out-of-band token
file; this generalizes the same hazard to the interactive `login`/`whoami`
path.)

**2. `.onAppear` on a `.menu`-style SwiftUI `MenuBarExtra` fires on menu-OPEN,
not at app launch.** Empirically verified here (launching the app fired no
`whoami` subprocess until the menu was opened). This makes `.onAppear` on
menu-bar content a **safe** lazy trigger for work that must not run at
launch — but it is not formally guaranteed by Apple, so pair it with a defensive
guard (here, `refreshIfNeeded`'s `status == .unknown` check) so that even if a
future macOS pre-rendered the menu at launch, no decrypt would occur.

## Prevention

- On macOS, audit every launch-path `.task`/`.onAppear`/init for calls that read
  Keychain/`keyring` state. If it decrypts a stored secret, move it behind a
  lazy, on-engagement trigger.
- Add a test pinning the invariant that constructing the auth controller does
  **no** auth work (`status == .unknown`, `whoamiCallCount == 0`) — the launch
  contract the fix rests on. See
  `macos/ScreenCapTests/CloudAuthControllerTests.swift`
  (`testInitialStateDoesNoAuthWork`).
- When deferring eager state, refresh **before** any gate that reads it
  (`attemptUpload` refreshes before checking `isSignedIn`) so a still-`.unknown`
  state can't misroute a signed-in user.
- Note: the macOS Swift test target isn't run in CI (CI runs only the Python
  privacy lane), so a Swift compile break can rot silently — this fix also had
  to repair a pre-existing `FakeCloudAuthService` conformance break from the
  billing commit `71a33bce`.

## References

- PR: proteus-computer-use/screencap#350
- Follow-up (durable storage-layer fix — shared Keychain access group so
  re-signed / cross-binary reads never prompt): SCR-241
- Same dev-build signing-churn root cause (TCC dimension): SCR-157
- Key files: `macos/ScreenCap/Controllers/CloudAuthController.swift`,
  `macos/ScreenCap/ScreenCapApp.swift`, `src/screencap/auth.py`
