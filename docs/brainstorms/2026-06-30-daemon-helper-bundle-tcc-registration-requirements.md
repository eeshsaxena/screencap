---
date: 2026-06-30
topic: daemon-helper-bundle-tcc-registration
---

# Daemon Helper-Bundle TCC Registration (SCR-196)

## Summary

Repackage the recording daemon as a proper, Developer-ID-signed helper **bundle with its own bundle identity** so macOS natively prompts for and auto-populates its TCC entries (Screen Recording, Accessibility, Input Monitoring) and `tccutil` can target it by bundle ID. The existing self-registration walkthrough is retained as a transitional fallback. The outcome: a brand-new user completes first-run setup without ever manually `+`-adding a deep binary path.

---

## Problem Frame

The recording daemon ships as a **bare Mach-O binary** at `Contents/Resources/screencap/screencap` (confirmed on the installed 0.1.0 build: code-signing `Identifier=screencap` — PyInstaller's filename default — `Info.plist=not bound`, `Sealed Resources=none`, `TeamIdentifier=2A8S6MV8DZ`, hardened runtime). There is no `com.screencap.daemon` bundle anywhere in the app.

macOS reserves TCC **auto-prompt / auto-population** and `tccutil` **bundle-ID targeting** for app bundles and properly-placed helpers that have a responsible-code ancestor. A bare nested executable qualifies for neither. So during onboarding, "Open Again" deep-links land the user on a Privacy pane with **nothing to toggle**, and `tccutil reset <service> screencap` / `… com.screencap.daemon` both fail with `No such bundle identifier` (reproduced live). Every new user — for a privacy-recording product whose very first action requires these grants — must navigate Finder into the app bundle to a deep binary path, across two separate panes, plus a Touch-ID auth that silently reverts if dismissed. High first-run drop-off risk.

A tactical workaround shipped (SCR-49 / U8, 2026-06-08): a daemon `permission.request` verb that self-registers each permission in the daemon's own process. It is insufficient: its only empirical validation (the 2026-06-05 spike) was run against an **ad-hoc** daemon identity, while the shipped artifact is **Developer-ID + hardened-runtime** ("no worse than ad-hoc" was assumed, never validated); Input Monitoring does not self-register from the request API at all (needs the event-tap path, only medium-confidence); and the Developer-ID distribution plan's reset/re-grant procedure assumes a bundle ID that the artifact does not have.

---

## Actors

- A1. New user (first run): installs the app, grants permissions, expects to record.
- A2. Existing tester (re-grant cohort): already granted permissions to the old bare-binary identity; will re-grant once after the identity change.
- A3. macOS TCC + System Settings: prompts, populates Privacy panes, and persists grants keyed on the subject's code identity.

---

## Key Flows

- F1. First-run grant (new user)
  - **Trigger:** A1 launches the app for the first time and reaches the "Set up ScreenCap" walkthrough.
  - **Actors:** A1, A3
  - **Steps:** Approve the helper (Login Items / SMAppService) → the daemon helper is installed and running under its bundle identity → the user grants each required permission → each deep-link lands on a Privacy pane with a real, toggleable entry for the helper → the user toggles it on.
  - **Outcome:** All required TCC entries exist and are enabled for the daemon's bundle identity; recording works without any manual `+`/deep-path add.
  - **Covered by:** R1, R2, R3, R5, R7

- F2. One-time re-grant (existing tester)
  - **Trigger:** A2 updates to the build where the daemon gains its bundle identity, orphaning prior grants.
  - **Actors:** A2, A3
  - **Steps:** User is warned in advance → updates → the walkthrough/recovery banner presents the three permissions as not-granted → user re-grants once → (restart/relaunch as needed for the per-process TCC cache).
  - **Outcome:** Grants restored against the new bundle identity; documented as a deliberate single re-grant release.
  - **Covered by:** R6, R8

---

## Requirements

**Helper packaging & identity**
- R1. The recording daemon must run as a TCC subject with a stable **bundle identifier** (`com.screencap.daemon`), Developer-ID signed, with a responsible-code ancestor — i.e. a real helper bundle, not a bare nested Mach-O. `codesign -dvv` on the shipped helper must report the bundle identifier (not `screencap`) and a bound `Info.plist`.
- R2. macOS must natively recognize the helper as a TCC subject: when the user reaches a Privacy pane for a required permission during onboarding, a real, **toggleable entry for the helper already exists** — no manual `+` / deep-binary-path add is required for any of the three required permissions.
- R3. The helper's identity must be `tccutil`-targetable by bundle ID, so reset/re-grant procedures (including the distribution plan's) work as written.

**Permission coverage**
- R4. All three required permissions — Screen Recording, Accessibility, Input Monitoring — must end up with a populated, toggleable native entry. Input Monitoring (the most visibly broken today) is explicitly in scope, not degraded to manual-only.

**Fallback retention**
- R5. The existing self-registration path (the `permission.request` verb and its registration touches) is **retained as a transitional fallback**, not removed. The native bundle path is primary; the fallback covers edge cases (e.g. older macOS, a pane reached before native population) during the transition.

**Migration & compatibility**
- R6. The identity change is shipped as a deliberate, **communicated one-time re-grant release**: existing testers are warned in advance, re-grant the three permissions once, and re-approve the helper in Login Items if SMAppService flags it. Stale daemon/socket from the prior identity must be cleared so the new helper registers cleanly rather than racing a surviving old daemon.
- R7. The daemon continues to be installed/launched via SMAppService/launchd; this is a packaging + identity change, not a switch to a different launch or supervision mechanism. The existing install flow must keep working against the new bundle.

**Validation**
- R8. On-device validation must be performed against the **shipped Developer-ID + hardened-runtime identity** (not only the ad-hoc identity used in the original spike) and on a **clean machine state** (no stale grant rows), confirming native registration for all three permissions and that post-grant recording works. This closes the original spike's validation gap and is an exit criterion.

---

## Acceptance Examples

- AE1. **Covers R2, R4.** Given a clean first-run machine with the helper installed and running, when the user opens the Screen Recording / Accessibility / Input Monitoring pane from the walkthrough, then each pane shows a toggleable entry for the helper that the user can flip on without using `+`.
- AE2. **Covers R3.** Given the helper is installed, when an operator runs `tccutil reset <service> com.screencap.daemon`, then the command succeeds (no "No such bundle identifier") and clears that grant.
- AE3. **Covers R5.** Given a macOS version or edge case where the native pane is reached before an entry is populated, when the fallback self-registration path runs, then a toggleable entry still appears (no regression versus today's behavior).
- AE4. **Covers R6.** Given an existing tester who previously granted permissions to the old bare-binary identity, when they update to the bundle-identity build, then the walkthrough/recovery surface presents the three permissions as not-granted and a single re-grant pass restores recording.

---

## Success Criteria

- A brand-new user completes first-run setup and records successfully **without ever manually adding a binary path** in System Settings.
- Input Monitoring shows a real, toggleable native entry during onboarding (not an empty pane).
- `tccutil` reset/re-grant by the daemon's bundle ID works, making the Developer-ID distribution plan's documented procedure executable as written.
- On-device validation on the shipped Developer-ID/hardened-runtime identity, clean state, confirms native registration for all three permissions — the spike's ad-hoc-only validation gap is closed.
- Downstream handoff: `ce-plan` can sequence the packaging, signing, install-flow, and migration work without having to re-decide product behavior, scope, or the migration posture.

---

## Scope Boundaries

- Removing the U8 self-registration workaround — deferred; revisit only after the bundle path is proven on-device (R5 keeps it for now).
- The org Team-ID flip, auto-update (e.g. Sparkle), and notarizing the standalone CLI tarball — owned by the Developer-ID / notarized-distribution plan (`docs/plans/2026-06-03-001-feat-macos-app-developer-id-notarized-distribution-plan.md`), not this work.
- Any change to the permission *model* (which permissions are required, the app-vs-daemon ownership split) — unchanged.
- Microphone permission flow — unchanged; remains app-owned.
- Redesign of the onboarding walkthrough UX — out; rows may have copy simplified once entries are native, but the flow's shape is not the subject.
- Cleaning the ~74 stale ad-hoc TCC rows on the dev machine — handled by existing dev-state teardown tooling, not this work.

---

## Key Decisions

- Pursue the structural fix (helper bundle) over hardening the workaround: the bare-binary identity is the root cause; a proper bundle makes the OS do auto-prompt/auto-populate and makes `tccutil`/the distribution plan's assumptions true, stopping recurrence. (Chosen: durability over speed.)
- Retain the self-registration path as a transitional fallback rather than removing it on cutover: lower transition risk for a permissions-critical first-run flow, accepted as temporary carrying cost.
- Accept and communicate a one-time tester re-grant: the identity change necessarily orphans prior grants; a single deliberate re-grant release is preferable to constraining the design to avoid churn.
- Keep SMAppService/launchd as the launch/registration mechanism: it already works for install/Login-Items; only the packaged subject's identity changes.

---

## Dependencies / Assumptions

- Developer-ID signing pipeline exists (helper is already Developer-ID signed today) and can sign an embedded helper *bundle* with a bound `Info.plist`, preserving the launchd/SMAppService exec path. (`docs/plans/2026-06-03-001-…`)
- PyInstaller produces a `--onedir` layout (the `screencap` Mach-O + `_internal`); wrapping that into a helper bundle with `Info.plist` + bundle ID must preserve the binary path the launcher and `CLIClient.resolveBinary()` resolve, and must avoid App Translocation.
- The 2026-06-05 spike confirmed native registration works for Screen Recording + Accessibility from the bare daemon identity on macOS 26.5.1; the open empirical question is whether the proper-bundle identity makes this reliable across panes (esp. Input Monitoring) and persistent — to be validated per R8.

---

## Outstanding Questions

### Deferred to Planning

- [Affects R1][Needs research] Exact helper-bundle shape and placement that yields a responsible-code ancestor while keeping SMAppService/launchd working (e.g. a `Contents/Library/...` LoginItems-style `.app` vs. another nested-bundle form) — validate on-device, don't decide from docs alone.
- [Affects R1][Needs research] How to wrap the PyInstaller `--onedir` output (bare Mach-O + `_internal`) into a signed bundle with a bound `Info.plist`/bundle ID without breaking the resolved binary path or hardened-runtime/library-validation.
- [Affects R4][Needs research] Whether the proper-bundle identity makes Input Monitoring populate natively (vs. still needing the event-tap touch retained via R5).
- [Affects R8][Technical] The clean-state, Developer-ID-identity validation procedure (fresh identity to avoid the stale-row treadmill, per the spike's `spikeclean` method), since it requires destructive TCC reset + a real device.
- [Affects R6][Technical] The precise cutover steps to evict the prior-identity daemon + stale socket and drive the single re-grant (relaunch for the per-process TCC cache) cleanly.
