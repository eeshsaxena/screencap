---
title: "feat: SCR-49 Phase 1c (U9) — TCC migration UX + entitlement-drop honesty"
type: feat
status: completed
date: 2026-06-29
origin: docs/plans/2026-05-08-001-feat-daemon-architecture-phase-1-plan.md
---

# feat: SCR-49 Phase 1c (U9) — TCC migration UX + entitlement-drop honesty

## Summary

Ship the deferred Phase 1c payoff of the daemon refactor: a one-time `DaemonMigrationView` banner (gated by a `~/.screencap/.tcc-migrated-v1` marker file) that explains the permission consolidation to existing users, plus the Developer-ID signing-validation runbook that gates the release. Two facts discovered during planning reshape the original U9 scope: (1) the SwiftUI app's `Info.plist`/entitlements **never declared** Screen Recording / Accessibility / Input Monitoring (macOS doesn't gate those TCC services via usage-string keys), so the "entitlement drop" is a verify-and-document task plus removal of a dead in-code app-process *request* branch — not a plist edit; and (2) the CLI-fallback recording path still depends on the app-process TCC probes, so those probes **stay** (the original "remove the three probes" instruction would have broken fallback recording). Net deliverable: the migration UX + marker + runbook + AE2 acceptance, with a faithful (not literal) entitlement-drop.

---

## Problem Frame

Phase 1a/1b shipped the daemon as the recording TCC subject but left SwiftUI's onboarding without the upgrade story: an existing user who already granted Screen Recording / Accessibility / Input Monitoring to the *app* gets silently re-prompted for the *daemon helper* on upgrade, with no explanation of why permissions appear to reset. Phase 1c closes that gap and realizes the strategic promise (AE2): once the daemon binary is signed with a stable Developer ID Application identity, TCC anchors its grants on `(identifier, Team ID)` and they survive rebuilds — so "grant once, persists across all future updates" becomes true. This was blocked on the Developer ID certificate; per planning confirmation the cert is now in hand, so the work is executable behind a pre-flight validation gate rather than blocked.

---

## Requirements

- R1. A one-time migration banner (`DaemonMigrationView`) explains the helper consolidation to existing users on upgrade, shown exactly once. *(origin: R5b)*
- R2. Banner display is gated by marker file `~/.screencap/.tcc-migrated-v1`; the marker is written only after the user completes (or dismisses past) the migration step. *(origin: R5b)*
- R3. The migration banner is suppressed while a recording is in progress (no stacking modal-feeling surfaces over an active capture). *(origin: U9 approach)*
- R4. The marker file suppresses only the *banner*; it is never treated as proof of grants. The daemon's startup TCC preflight independently emits `permission_lost` when grants are actually missing (spoofed/stale marker is harmless). *(origin: U9 approach)*
- R5. The SwiftUI app declares **no** Screen Recording / Accessibility / Input Monitoring TCC entitlements or usage-strings, and no longer *requests* them as the app's own TCC subject on the daemon (happy) path. The daemon's bundled binary is the sole TCC subject on that path. *(origin: R5b)*
- R6. The CLI-fallback recording path (daemon unreachable) is preserved; its app-process probes and start-gate remain intact. *(planning decision — see Key Technical Decisions)*
- R7. A signing-validation runbook documents the Developer ID build → notarize → install → grant → rebuild → verify-persistence pipeline, and the pre-flight gate passes before code ships. *(origin: U9 pre-flight gate)*
- R8. AE2 acceptance holds: TCC grants on the daemon binary persist across ≥5 consecutive SwiftUI+daemon rebuilds with the Developer ID identity, without re-prompt.
- R9. Release notes call out the migration UX ("grant once, persists across updates"). *(origin: U9 acceptance)*

**Origin actors:** A1 (existing Screencap user upgrading), A2 (sole developer / release engineer running the signing pipeline).
**Origin flows:** F-upgrade (launch-after-upgrade migration sequence: marker check → banner → U8 install+grant → marker write → normal flow).
**Origin acceptance examples:** AE2 (covers R5, R8 — TCC grants persist across rebuilds; the strategic payoff).

---

## Scope Boundaries

- **Not** removing the app-process TCC probes (`checkScreenRecording` / `checkAccessibility` / `checkInputMonitoring`, `refresh()`, `allRequiredGranted`). They gate the still-supported CLI-fallback path. The original U9 "PermissionController cleanup removes the three probes" is **superseded** by this boundary (see Key Technical Decisions).
- **Not** editing `Info.plist` / `Screencap.entitlements` to *remove* Screen Recording / Accessibility / Input Monitoring keys — they were never present. U4 verifies and documents this rather than editing.
- **Not** reworking the daemon's TCC registration, the U8 install flow, or the permission walkthrough rows — those shipped in Phase 1b and are reused as-is.
- **Not** automating the AE2 smoke into CI — that remains a manual runbook step (a follow-up ticket already exists in the parent plan's Future Work for `tccutil`-based CI automation).
- **Not** the daemon-mandatory architecture (dropping CLI-fallback entirely). Confirmed out of scope; CLI-fallback stays.

### Deferred to Follow-Up Work

- CI automation of the AE2 rebuild-persistence check (`tccutil`-database query pre/post rebuild): separate ticket per the parent plan's Future Work note.
- Individual→org Team ID migration re-grant UX: if the in-hand Developer ID is the *individual* membership and an org switch is still planned (see `docs/brainstorms/2026-06-03-individual-apple-dev-membership-tester-distribution-requirements.md`), the org flip forces a one-time re-grant. The `DaemonMigrationView` machinery built here is the natural vehicle, but bumping the marker to `.tcc-migrated-v2` and re-showing the banner is a future iteration, not this release.

---

## Context & Research

### Relevant Code and Patterns

- `macos/Screencap/Controllers/PermissionController.swift` — owns app-process probes (`checkScreenRecording`/`checkAccessibility`/`checkInputMonitoring`, `refresh`, `allRequiredGranted`) **and** the daemon-grant snapshot path. The dead app-process *request* branch is `requestAndOpenSettings(for:subject:)`'s `.screenCapApp` case (lines ~537–554): `CGRequestScreenCaptureAccess` / `AXIsProcessTrustedWithOptions(prompt:true)` / `IOHIDRequestAccess`. Its only caller passes `.daemon`, so the branch is unreachable.
- `macos/Screencap/Views/Privacy/FirstRunPermissionsView.swift` — the U8 walkthrough: daemon-install step → three daemon-permission rows. No longer polls app-process TCC. The migration banner is a new step *before* this content on the upgrade path.
- `macos/Screencap/Views/MainWindow.swift` — `FirstRunSetupPresentationPolicy` + `updateFirstRunSheetPresentation()` own when the first-run sheet shows/auto-closes, and already guard against popping over an active recording (`recorder.state.isRecording`). The migration banner integrates into this presentation policy, reusing the active-recording guard (R3).
- `macos/Screencap/ScreencapApp.swift` — `.task` after `probeDaemon()` runs `privacy.ensureFirstLaunchModeWritten()`, the model for a one-time on-disk first-launch action; mirror it for the marker.
- `macos/Screencap/Controllers/PrivacyController.swift:169` — `ensureFirstLaunchModeWritten()` is the idempotent "probe disk, short-circuit if already done, else write" pattern to mirror for the marker store.
- `macos/Screencap/Controllers/DaemonClient.swift:406` — `~/.screencap/...` paths are built ad hoc via `NSHomeDirectory()`; there is no central Swift `AppPaths` helper. The marker path follows the same idiom.
- `macos/Screencap/Controllers/DaemonInstallController.swift` / `DaemonSessionService.swift` — `snapshot()` exposes `isRecording`, the signal for the R3 banner suppression (mirror `RecorderController.state.isRecording`, which `MainWindow` already uses).
- `docs/plans/2026-05-08-001-feat-daemon-architecture-phase-1-plan.md` — U9 scope (lines 663–710), the authoritative origin for this plan.

### Institutional Learnings

- `docs/solutions/build-errors/macos-ad-hoc-signing-tcc-rebuild-treadmill.md` — the load-bearing learning. TCC anchors grants on the code-signing identity; ad-hoc builds re-key on every rebuild. The **2026-06-08 update** is critical and refines the original U9 framing: `embed-cli.sh` now team-signs the *nested* `screencap` binary (the real TCC subject), and an **Apple Development** cert already stops the per-build dev treadmill (the Designated Requirement pins identifier + leaf cert CN). **Developer ID** is the distribution-grade anchor (team-anchored DR, survives cert rotation). The runbook (U1) builds directly on this. Per-pane name divergence ("Screencap" vs lowercase `screencap`), default-OFF + stale-pane, and Input-Monitoring-not-self-registering-under-launchd are documented gotchas the runbook must carry.
- `docs/solutions/build-errors/env-export-prefix-silently-disables-team-signing.md` — a `.env` `export ` prefix silently dropping `DEVELOPMENT_TEAM` lands you back on ad-hoc; the runbook must warn about it.

### External References

- None required — this is internal macOS TCC / code-signing behavior, fully covered by the local learning docs and the parent plan. (External research skipped: strong local patterns, no new framework surface.)

---

## Key Technical Decisions

- **CLI-fallback path is retained; app-process probes stay.** Confirmed during planning. The app remains a *fallback* TCC subject when the daemon is unreachable, so `checkScreenRecording`/`checkAccessibility`/`checkInputMonitoring` + `allRequiredGranted` + the `RecorderController` cliFallback start-gate and `PermissionWatchdog` re-check stay intact. This **supersedes** the parent plan's "remove the three probes" U9 line, which predated the Phase 1b CLI-fallback hardening and would have broken fallback recording. *(origin tension: parent plan U9 "PermissionController cleanup")*
- **"Entitlement drop" is satisfied faithfully, not literally.** The three TCC services were never declared in `Info.plist`/entitlements (git history confirms; macOS doesn't use usage-string keys for them). R5 is met by (a) verifying/documenting that the app declares none of them, and (b) removing the dead in-code `.screenCapApp` *request* branch so the app never registers itself as a Screen Recording / Accessibility / Input Monitoring TCC subject on the daemon path. The daemon binary is the sole subject on the happy path; CLI-fallback's probe-based gate is a documented, deliberate exception.
- **Marker store mirrors `ensureFirstLaunchModeWritten`.** A small, injectable marker store (idempotent disk probe + write at `~/.screencap/.tcc-migrated-v1`) rather than `@AppStorage`/`UserDefaults`, so it is cross-process visible and lives alongside the daemon's own `~/.screencap` state (consistent with the rest of the app's on-disk conventions). `NSHomeDirectory()`-based path, hardened against a non-existent `~/.screencap` (create-if-needed).
- **Banner integrates into the existing first-run presentation policy**, not a parallel sheet. `FirstRunSetupPresentationPolicy` already owns sheet gating and the active-recording guard; the migration step is a leading phase of the existing sheet content, gated by the marker, so there is one presentation surface (avoids the documented two-sheet presentation-race hardening already in `MainWindow`).
- **Marker is advisory for UX only (R4).** Never a grant oracle. The daemon's `_check_macos_permissions()` startup path remains the authority; a spoofed/stale marker only suppresses the banner and still surfaces `permission_lost` if grants are missing.

---

## Open Questions

### Resolved During Planning

- *Are the app-process probes removable?* No — CLI-fallback depends on them. Resolved: keep them (R6).
- *Is this still blocked on the cert?* No — Developer ID is in hand. Resolved: executable behind the U1 pre-flight gate.
- *Does the entitlement drop require a plist edit?* No — the keys were never present. Resolved: verify-and-document + dead-branch removal (R5, U4).

### Deferred to Implementation

- *Marker store as a free function vs. a small type injected into the migration view-model* — settle when wiring U2's testability seam; both satisfy the idempotent-write contract.
- *Exact placement of the marker-write call* (on banner "Continue" vs. after the U8 install+grant step completes) — the F-upgrade sequence says "write after install+grant"; confirm against the actual `DaemonInstallController` completion signal during U3 so a user who quits mid-install doesn't get a marker without grants. Leaning: write after the install step reports `installedAndRunning`, matching origin step 4.
- *Is the in-hand Developer ID the individual or org membership?* Affects only the deferred `.tcc-migrated-v2` follow-up, not this release.

---

## High-Level Technical Design

> *This illustrates the intended approach and is directional guidance for review, not implementation specification. The implementing agent should treat it as context, not code to reproduce.*

Upgrade-launch sequence (F-upgrade), reusing the existing first-run sheet surface:

```
app launch (post-upgrade)
        │
        ▼
  probeDaemon()  ── transport = .daemon | .cliFallback
        │
        ▼
  MarkerStore.isMigrated("~/.screencap/.tcc-migrated-v1")?
     ├─ yes ─────────────────────────────► existing first-run/launch gate (unchanged)
     └─ no
         │
         ▼
   recording in progress? ── yes ─► suppress banner this launch (R3); re-evaluate next launch
         │ no
         ▼
   show DaemonMigrationView (one-time banner, leading step of the sheet)
         │  user taps "Continue"
         ▼
   U8 daemon install + TCC walkthrough (existing FirstRunPermissionsView content)
         │  daemonInstaller.state == .installedAndRunning
         ▼
   MarkerStore.write()  ── banner never shows again (R2)
         │
         ▼
   normal flow
```

Decision matrix for whether the migration banner shows on a given launch:

| marker present | recording active | result |
|---|---|---|
| yes | — | no banner (migration done) |
| no | yes | no banner this launch (suppressed, R3) |
| no | no | show banner (one-time) |

---

## Implementation Units

### U1. Developer ID signing-validation runbook + pre-flight AE2 gate

**Goal:** Author `docs/runbooks/developer-id-signing-validation.md` and execute it end-to-end with the in-hand Developer ID Application certificate, proving TCC grants on the daemon binary survive a rebuild before any Phase 1c code ships. This is the gate for U2–U5.

**Requirements:** R7 (partial R8 — single-cycle validation; full 5× in U5).

**Dependencies:** None (the cert is in hand). Must complete and pass before U2–U5 ship.

**Files:**
- Create: `docs/runbooks/developer-id-signing-validation.md`

**Approach:**
- Document the pipeline: build `screencap` binary with the Developer ID Application identity → notarize (manual or CI) → install on a clean macOS 13+ machine → grant Screen Recording → rebuild with the same identity → reinstall → verify the grant persists without re-prompt.
- Carry forward the gotchas from `macos-ad-hoc-signing-tcc-rebuild-treadmill.md`: verify the *nested* `Contents/Resources/screencap/screencap` binary's signature (not just the app) with `codesign -dvv`; confirm the Designated Requirement is identity-anchored (not cdhash); per-pane name divergence; default-OFF + stale-pane (`Cmd+Q` System Settings to refresh); Input Monitoring not self-registering under launchd; the `.env` `export `-prefix `DEVELOPMENT_TEAM` foot-gun.
- Document the abort condition: if any step fails (notarization rejected, grant clobbers on rebuild), **do not proceed to U2** — surface as a release-engineering blocker.

**Patterns to follow:**
- Existing runbooks: `docs/runbooks/cloud-migration-runbook.md`, `docs/runbooks/gcp-migration-proteus-photos.md` (structure, step numbering, verification-command style).

**Test scenarios:**
- Test expectation: none — documentation + manual validation. The validation *is* the test: executing the runbook once and confirming the grant persists across the single rebuild cycle is U1's exit criterion. (Full 5× acceptance is U5.)

**Verification:**
- Runbook exists, is followed end-to-end, and the daemon-binary Screen Recording grant survives a rebuild-reinstall with no re-prompt. If it fails, U2–U5 do not start.

---

### U2. Migration marker store + `DaemonMigrationView` one-time banner

**Goal:** Build the `~/.screencap/.tcc-migrated-v1` marker store (idempotent read/write) and the `DaemonMigrationView` banner that explains the helper consolidation.

**Requirements:** R1, R2, R4.

**Dependencies:** U1 (gate passed).

**Files:**
- Create: `macos/Screencap/Views/Privacy/DaemonMigrationView.swift`
- Create: `macos/Screencap/Controllers/MigrationMarkerStore.swift` (or a small marker helper — name settled in implementation)
- Test: `macos/ScreencapTests/MigrationMarkerStoreTests.swift` (and a `DaemonMigrationView` snapshot/logic test if the view carries non-trivial state)

**Approach:**
- `MigrationMarkerStore`: `isMigrated()` (probe `~/.screencap/.tcc-migrated-v1`) and `markMigrated()` (create `~/.screencap` if absent, write the marker). Mirror `PrivacyController.ensureFirstLaunchModeWritten`'s idempotent-disk-probe shape. Path via `NSHomeDirectory()` per `DaemonClient.swift:406`. Inject the base directory for tests (point at a temp dir) so tests never touch the real `~/.screencap`.
- `DaemonMigrationView`: copy per origin — "Screencap now uses a background helper for stable permissions across updates. Grant permissions once and they'll persist across all future Screencap updates." A single "Continue" affordance that advances to the U8 install/walkthrough content. No grant logic of its own (R4 — marker ≠ grant proof).

**Patterns to follow:**
- `PrivacyController.ensureFirstLaunchModeWritten()` (idempotent first-launch disk write).
- `FirstRunPrivacyBanner.swift` / `FirstRunPermissionsView.swift` (banner/step visual idiom, accessibility-hidden decorative icons, copy tone).

**Test scenarios:**
- Happy path: `isMigrated()` returns false on a clean temp dir; after `markMigrated()`, returns true.
- Happy path: `markMigrated()` creates `~/.screencap` when the directory is absent, then writes the marker.
- Edge case: `markMigrated()` called twice is idempotent (no error, marker still present).
- Edge case: marker present but injected dir read-only / write fails → `markMigrated()` surfaces/logs the failure without crashing (the banner re-showing once next launch is the harmless fallback, R4).
- Edge case (view): `DaemonMigrationView` renders the consolidation copy and exposes exactly one "Continue" action; no permission probes invoked.

**Verification:**
- Marker round-trips on disk; `DaemonMigrationView` renders and advances without touching TCC state.

---

### U3. Wire the migration banner into the upgrade/first-run presentation flow

**Goal:** Show `DaemonMigrationView` once, before the U8 install + TCC walkthrough, on the upgrade path; suppress it during active recording; write the marker after the install step completes.

**Requirements:** R1, R2, R3.

**Dependencies:** U2.

**Files:**
- Modify: `macos/Screencap/Views/Privacy/FirstRunPermissionsView.swift` (insert the migration step as the leading phase before `daemonInstallStep`)
- Modify: `macos/Screencap/Views/MainWindow.swift` (`FirstRunSetupPresentationPolicy` / `updateFirstRunSheetPresentation()` consult the marker; reuse the existing `recorder.state.isRecording` guard)
- Modify: `macos/Screencap/ScreencapApp.swift` (read the marker in the post-`probeDaemon` `.task`, alongside `ensureFirstLaunchModeWritten`, to decide migration state)
- Test: `macos/ScreencapTests/FirstRunSetupPresentationPolicyTests.swift` (extend existing presentation-policy tests with the marker dimension)

**Approach:**
- Add a `migrationNeeded` (marker-absent) input to the first-run sheet content: when true and not recording, the sheet leads with `DaemonMigrationView`; "Continue" advances to the existing daemon-install content.
- Reuse the active-recording suppression already in `updateFirstRunSheetPresentation()` (`if recorder.state.isRecording { return }`) so R3 needs no new guard — the migration step inherits it.
- Write the marker when the daemon install reports `installedAndRunning` (origin step 4), via the existing `.onChange(of: daemonInstaller.state)` seam in `FirstRunPermissionsView`. Confirm this placement avoids marking migrated when the user quits mid-install (Deferred-to-Implementation note).
- Do not disturb the recovery latch (`reopenSetupRequested`) or auto-close (`shouldAutoCloseOnUpdate`) behavior — the migration step is purely additive to the launch path.

**Patterns to follow:**
- `FirstRunSetupPresentationPolicy.shouldPresentOnLaunch` / `shouldAutoCloseOnUpdate` (pure, testable gate functions — keep the new marker logic in the same pure-function style for unit testing without a live window).
- The existing `.onChange(of: daemonInstaller.state)` install-complete seam.

**Test scenarios:**
- Happy path: marker absent + daemon path + not recording → migration banner presented as the leading step; after install completes, marker written; subsequent launch with marker present → no banner.
- Happy path: marker present → presentation policy behaves exactly as today (no migration step), CLI-fallback and daemon gates unchanged.
- Edge case: marker absent + recording in progress → no banner this launch (R3); re-evaluated and shown on a later non-recording launch.
- Edge case: user dismisses ("Skip for now") before completing install → marker NOT written (no false migration), banner eligible to re-show next launch.
- Integration: recovery-latch reopen (`reopenSetupRequested`) with marker already present → opens the walkthrough without re-showing the migration banner (migration is one-time, recovery is separate).

**Verification:**
- Banner shows once on a marker-absent, non-recording launch; never over an active recording; never after the marker is written. Existing first-run/CLI-fallback/recovery behavior is unchanged when the marker is present.

---

### U4. Entitlement-drop verification + dead app-process request-branch removal

**Goal:** Honor R5 faithfully: verify the app declares no Screen Recording / Accessibility / Input Monitoring entitlements or usage-strings, document why there is nothing to edit, and remove the dead `.screenCapApp` request branch so the app never registers itself as a TCC subject on the daemon path. Keep the app-process *probes* (CLI-fallback).

**Requirements:** R5, R6.

**Dependencies:** U1 (gate passed). Independent of U2/U3 — can land in parallel.

**Files:**
- Modify: `macos/Screencap/Controllers/PermissionController.swift` (remove the unreachable `.screenCapApp` case body in `requestAndOpenSettings(for:subject:)` — `CGRequestScreenCaptureAccess` / `AXIsProcessTrustedWithOptions(prompt:true)` / `IOHIDRequestAccess`; collapse the signature to daemon-only if the `subject` param becomes vestigial). **Keep** `checkScreenRecording`/`checkAccessibility`/`checkInputMonitoring`, `refresh()`, `allRequiredGranted`.
- Verify (no edit expected): `macos/Screencap/Info.plist`, `macos/Screencap/Screencap.entitlements`, `macos/project.yml` — confirm none declare the three TCC services.
- Test: `macos/ScreencapTests/PermissionControllerTests.swift` (assert CLI-fallback probe behavior intact; assert no app-process screen-recording request path remains)

**Approach:**
- Confirm via `requestAndOpenSettings`'s single caller (`FirstRunPermissionsView`, `subject: .daemon`) that the `.screenCapApp` branch is dead, then remove it. If `PermissionSubject`/the `subject` parameter is left with a single inhabitant, simplify rather than leave a vestigial enum — but only if it doesn't ripple into test seams; otherwise leave the enum and just empty/guard the dead branch.
- Add a short comment block (and a line in the runbook or `macos/README.md`) recording *why* there is no plist edit: macOS tracks Screen Recording / Accessibility / Input Monitoring via TCC at request time, not via `NS*UsageDescription` keys, so the app declaring none of them already satisfies "the app requests none of the three." This pre-empts a future reader "fixing" a non-bug.
- Explicitly preserve the CLI-fallback probe surface and add a comment tying it to R6 so a later cleanup pass doesn't re-introduce the removed-instruction risk.

**Patterns to follow:**
- The existing `#if DEBUG` test seam and injectable initializers in `PermissionController` (keep the probes testable).

**Test scenarios:**
- Happy path: CLI-fallback start-gate still reads `allRequiredGranted` from the live probes (behavior unchanged) — assert via the existing `_testSetRequiredPermissionsGranted` seam.
- Edge case: `requestAndOpenSettings(for:subject:.daemon)` still drives the daemon registration round-trip (unchanged); no app-process prompt API (`CGRequestScreenCaptureAccess` etc.) is reachable from any caller.
- Verification (non-behavioral): `Info.plist` / `Screencap.entitlements` / `project.yml` declare none of the three TCC services — assert by inspection / a lightweight string-absence check if a plist test fixture exists.

**Verification:**
- App declares and requests none of the three TCC services as its own subject on the daemon path; CLI-fallback probe gate unchanged; plist/entitlements confirmed clean with a documented rationale.

---

### U5. AE2 acceptance smoke (5× rebuild) + release notes

**Goal:** Final acceptance — run the AE2 manual smoke (5 consecutive Developer-ID rebuilds, grants persist) and write the release-notes entry calling out the migration UX.

**Requirements:** R8, R9.

**Dependencies:** U2, U3, U4 (the full migration UX must be in place); U1 (runbook is the procedure).

**Files:**
- Modify: the release-notes / changelog surface (e.g. `CHANGELOG.md` or the release-notes doc the repo uses — confirm during implementation)
- Test: extend `docs/runbooks/developer-id-signing-validation.md` with the 5× AE2 acceptance section (or reference it from the runbook)

**Approach:**
- Execute the AE2 smoke: rebuild the SwiftUI app + daemon binary five times in a row with the Developer ID identity; after each rebuild, confirm the daemon binary's Screen Recording / Accessibility / Input Monitoring grants persist without re-prompt (per the runbook's verification commands). Record the result in the runbook.
- Write release notes: "Screencap now uses a background helper for stable permissions across updates. Grant permissions once and they'll persist across all future Screencap updates." Note the forward-only nature internally (not user-facing).

**Patterns to follow:**
- The parent plan's Success Metrics framing for AE2 (≥5 rebuilds, no re-prompt).

**Test scenarios:**
- Integration (manual, covers AE2): 5 rebuild cycles → grants persist across all 5. This is the load-bearing acceptance test.
- Edge case: if any cycle re-prompts, treat as a signing-pipeline regression — halt the release, file a signing-pipeline sub-issue (per origin "Suggested next step").

**Verification:**
- AE2 smoke passes (5/5 cycles, no re-prompt); release notes published; the forward-only commitment is acknowledged in the release record.

---

## System-Wide Impact

- **Interaction graph:** Adds one new on-disk artifact (`~/.screencap/.tcc-migrated-v1`) read at launch (`ScreencapApp.task`) and written at install-complete (`FirstRunPermissionsView`). Touches the first-run sheet presentation policy (`MainWindow`) — the single highest-traffic onboarding surface — but only additively (a leading step when the marker is absent).
- **Error propagation:** Marker read/write failures are non-fatal (R4) — a failed write means the banner re-shows once, a failed read defaults to "show the banner" (fail-toward-informing-the-user). The daemon's `permission_lost` event path is unchanged and remains the grant authority.
- **State lifecycle risks:** Stale/spoofed marker → banner suppressed but grants independently verified (harmless). Marker written before grants complete → mitigated by writing only on `installedAndRunning` (U3 placement decision). User quits mid-install → no marker, banner eligible next launch.
- **API surface parity:** No daemon API change, no CLI change, no recording-data-model change. Pure SwiftUI-shell + docs change.
- **Unchanged invariants:** CLI-fallback recording path and its app-process probe gate (R6); the U8 daemon-install + walkthrough flow; the recovery latch (`reopenSetupRequested`) and auto-close (`shouldAutoCloseOnUpdate`) behaviors; `Info.plist`/entitlements (verified, not edited).

---

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| Removing the dead `.screenCapApp` branch accidentally trims a probe the CLI-fallback path needs | U4 explicitly preserves `checkScreenRecording`/`checkAccessibility`/`checkInputMonitoring`/`refresh`/`allRequiredGranted`; tests assert the CLI-fallback gate is intact; a comment ties them to R6. |
| Marker written before grants land (false "migrated") | Write marker only on `daemonInstaller.state == .installedAndRunning` (U3); "Skip for now" never writes it. |
| Banner stacks over an active recording | Reuse the existing `recorder.state.isRecording` guard in `updateFirstRunSheetPresentation()` (R3) — no new guard needed. |
| Phase 1c is forward-only — rollback re-prompts already-migrated users | U1 pre-flight gate must pass before shipping; AE2 5× smoke (U5) is the final guard; treat as a one-way release, fix-forward. |
| In-hand Developer ID is *individual*; future org switch forces a re-grant | Out of scope for this release; the `.tcc-migrated-v2` re-show is deferred follow-up (see Scope Boundaries) — the marker machinery built here is the vehicle. |
| Notarization rejected at release time | U1 runbook includes notarization; do not ship un-notarized; halt and investigate per the origin risk table. |

### Dependencies / Prerequisites

- **Developer ID Application certificate — in hand** (confirmed during planning). U1 validates it end-to-end before any code ships.
- macOS 13+ deployment floor (`macos/project.yml`).
- The Phase 1b U8 daemon-install + walkthrough flow shipped (it has — `FirstRunPermissionsView` is daemon-grant-based).

---

## Documentation / Operational Notes

- New runbook `docs/runbooks/developer-id-signing-validation.md` (U1) becomes the canonical signing/notarization/TCC-persistence procedure for every future release, not just this one.
- Update `macos/README.md`'s "TCC permissions on dev builds" section (and/or `macos-ad-hoc-signing-tcc-rebuild-treadmill.md` Related links) to point at the new runbook and record that the app intentionally declares no Screen Recording / Accessibility / Input Monitoring entitlements (U4 rationale).
- Release notes (U5) call out the migration UX. The forward-only nature is an internal release-record note, not user-facing.

---

## Sources & References

- **Origin document:** [docs/plans/2026-05-08-001-feat-daemon-architecture-phase-1-plan.md](docs/plans/2026-05-08-001-feat-daemon-architecture-phase-1-plan.md) — U9 scope (lines 663–710), Phased Delivery (Phase 1c), Risks & Success Metrics (AE2).
- Linear issue: [SCR-49](https://linear.app/zk-email/issue/SCR-49/phase-1c-u9-swiftui-entitlement-drop-tcc-migration-ux-blocked-on)
- Upstream brainstorm: docs/brainstorms/2026-05-08-cli-gui-mcp-architecture-requirements.md (AE2 strategic payoff)
- Related brainstorm (cert/notarization + future org switch): docs/brainstorms/2026-06-03-individual-apple-dev-membership-tester-distribution-requirements.md
- Learnings: docs/solutions/build-errors/macos-ad-hoc-signing-tcc-rebuild-treadmill.md, docs/solutions/build-errors/env-export-prefix-silently-disables-team-signing.md
- Key code: macos/Screencap/Controllers/PermissionController.swift, macos/Screencap/Views/Privacy/FirstRunPermissionsView.swift, macos/Screencap/Views/MainWindow.swift, macos/Screencap/ScreencapApp.swift, macos/Screencap/Controllers/PrivacyController.swift
