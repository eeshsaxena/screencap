---
title: "feat: Foolproof macOS permission onboarding for the Screencap helper"
type: feat
status: active
date: 2026-06-30
deepened: 2026-06-30
origin: docs/brainstorms/2026-06-30-foolproof-macos-permission-onboarding-requirements.md
---

# feat: Foolproof macOS permission onboarding for the Screencap helper

## Summary

Make the recording daemon's macOS permission rows impossible to get wrong: prove the cheapest lever that makes the Privacy-pane row read "Screencap" (plist keys + cache refresh first; same-name bundle rename only if the filename is the true lever), pre-register the Screen Recording + Accessibility rows daemon-side at install so the user only ever toggles, remove decoy/orphan rows, and block setup with a Retry if a row never appears.

---

## Problem Frame

Recording runs in a hidden daemon helper (`com.screencap.daemon`) inside `Screencap.app`; macOS grants are per-identity, so the user must grant the daemon, not the visible app. Today the daemon's Privacy row is created only on-demand (when the user clicks "Grant"), the row is labeled by the wrong string, and the panes accumulate look-alike decoys — so a non-technical user is forced into a manual "+" add and grants the visible app instead, leaving the daemon ungranted and recording silently degraded. See origin `docs/brainstorms/2026-06-30-foolproof-macos-permission-onboarding-requirements.md` for the full problem narrative and on-device diagnosis.

---

## Requirements

- R1. Daemon registers its Screen Recording + Accessibility rows proactively at helper install/approval, **additive to and earlier than** the retained on-demand registration. (origin R1)
- R2. Onboarding never requires the System Settings "+" / manual-add control — the user's only action per permission is flipping an existing toggle. (origin R2)
- R3. The daemon's Privacy-pane row reads **"Screencap"**, and the onboarding copy names that exact row. (origin R3)
- R4. The walkthrough visually identifies the row with the helper's **icon** + the exact name. (origin R4)
- R5. Exactly one "Screencap" row exists per pane — orphaned old-identity and legacy app-identity rows are removed (or never created). (origin R5)
- R6. The app (`com.screencap.macos`) never appears in the Screen Recording or Accessibility panes — only the daemon's row does. (origin R6)
- R7. If the expected row fails to appear after registration, setup **blocks** with a clear "couldn't set this up" state + **Retry**; never silently proceeds, never falls back to manual-add. (origin R7)
- R8. The user can revoke by toggling off the same single "Screencap" row. (origin R8)
- R9. On upgrade, rows are relabeled/cleaned **without forcing re-grant** (existing grants preserved). (origin R9)

**Origin actors:** A1 (non-technical user), A2 (Screencap app — GUI shell, triggers registration, reads daemon state), A3 (Screencap daemon — capture process + TCC subject, the only actor that can self-register and authoritatively report grant state).
**Origin flows:** F1 (first-run grant), F2 (registration-failure fallback).
**Origin acceptance examples:** AE1 (covers R1, R2, R3), AE2 (covers R5, R6), AE3 (covers R7), AE4 (covers R8).

---

## Scope Boundaries

- **Input Monitoring** — already demoted to optional (PR #315); it cannot be registered for the helper on macOS 26.x and must stay out of the required flow.
- **Reversing daemon-as-TCC-subject** (making the app the permission identity) — rejected; the daemon stays the subject (origin Key Decisions; consistent with the Developer-ID distribution plan).
- **System-dialog / avoid-per-row-toggling reframe** — not chosen.
- **Re-litigating the on-demand-vs-every-boot registration debate** — the SCR-196 brainstorm rejected every-boot self-registration; this plan adds *one* install-time registration and **retains** the on-demand `permission.request` path as a fallback. Do not remove on-demand.

### Deferred to Follow-Up Work

- SCR-199 (orphaned-row cleanup) is **absorbed** into U4 here — close/fold it rather than working it separately.

---

## Context & Research

### Relevant Code and Patterns

- `pyinstaller/screencap.spec` (lines ~280-297) — the helper `BUNDLE()` def. `name='ScreencapDaemon.app'` controls the `.app` filename; `CFBundleName`/`CFBundleDisplayName='Screencap Helper'`; `CFBundleExecutable='screencap'` must equal the inner `EXE` name; `bundle_identifier='com.screencap.daemon'` (must stay). `icon=None` (R4 needs an icon).
- `macos/Screencap/Scripts/embed-cli.sh` — embeds + signs the helper inside-out (no `--deep`). Hardcoded `ScreencapDaemon.app` at L24/L27/L29, and inside **literal `<<'EOF'` heredocs** at L92/L101 (launcher `exec` path) plus warnings L110/L116/L127-130.
- `script/sign_app.sh` (L66-67 path; L131 `--verify --deep --strict`; L143 DR gate naming `com.screencap.daemon`) — standalone CI signer, same inside-out order.
- `script/notarize_app.sh` — operates on the whole `.app`/DMG; **no helper-filename literal** (rename-transparent). (Origin's `macos/notarize_app.sh` path was wrong.)
- `macos/Screencap/Resources/com.screencap.daemon.plist` — launchd plist. `Label=com.screencap.daemon`, `BundleProgram=Contents/Resources/screencap-daemon-launcher` (a launcher script, **not** the helper bundle). **Rename-immune.**
- `macos/Screencap/Controllers/DaemonInstallController.swift` — install state machine. `installedAndRunning` posts `Notification.Name.screenCapDaemonInstalledAndRunning` (the proactive-registration hook); `SMAppService.agent(plistName:)` is keyed on the plist name (rename-immune). Version reconciliation (SCR-121/135): `bootout()` + re-register on `versionMismatch`.
- `src/screencap/daemon/launchagent.py` — the **headless/CLI** install path (`launchctl bootstrap`/`kickstart`); parallel to the Swift SMAppService path. No registration hook today.
- `src/screencap/daemon/permission_register.py` — `register_permission(perm)` runs the request API **in the daemon's own process** (responsible-process constraint). The function a proactive trigger calls.
- `src/screencap/daemon/permission_probe.py` — fresh-subprocess grant probe (tri-state). Reports **grant state, not row existence** — R7 needs a new signal.
- `src/screencap/daemon/app.py` — `/v0/permission.request` verb (validates `PERMISSION_KEYS`, runs `register_permission` off-loop, audited); `daemon.info` grant block.
- `macos/Screencap/Controllers/PermissionController.swift` — `requestDaemonPermission` (on-demand Grant path), `PrivacyPane.helperSettingsEntryName` (returns "Screencap Helper" for the 3 daemon panes — must become "Screencap"), `PermissionSubject.bundleIdentifier`.
- `macos/Screencap/Views/Privacy/FirstRunPermissionsView.swift` — walkthrough; `daemonPermissionRow` (icon via `grantRowIcon`, copy via `daemonRationale`), the ~5s daemon-grant watch loop, the install-step `installFailed`/Retry pattern (model for R7's row-level Retry).
- `macos/Screencap/Controllers/CLIClient.swift` (L121-126 `resolveBinary()`) — hardcoded helper path; changes on rename.
- `tests/daemon/test_launchagent.py` (L106-117 plist renderer — rename-immune; L135-138 launcher exact-string — **breaks on rename**).

### Institutional Learnings

- `docs/solutions/build-errors/daemon-tcc-identity-migration-2026-06-30.md` (U7) — TCC keys grants on the `csreq` (bundle-id + Team), **not** the filename; `tccutil reset All screencap` clears the orphan old identity without touching `com.screencap.daemon`; the launchd Label + bootout/version-reconciliation handle cutover.
- `docs/research/2026-06-30-daemon-helper-bundle-ondevice-validation.md` (U8 runbook) — the acceptance gate: confound-free (launchd, never shell-run), quarantined/translocated download, clean-state machine. Checkpoint A: `codesign -d -r-` must name `com.screencap.daemon` + Team.
- `docs/research/2026-06-05-daemon-tcc-registration-spike.md` — SR via `CGRequestScreenCaptureAccess()`, Accessibility via `AXIsProcessTrustedWithOptions({prompt:true})`, both daemon-side under launchd on 26.5.1, **default OFF**; freshly-registered rows are OFF and the SR pane does **not** live-refresh; requests must originate daemon-side (responsible-process attribution) or they attribute to the app (breaks R6).
- `docs/solutions/build-errors/macos-ad-hoc-signing-tcc-rebuild-treadmill.md` — **per-pane name divergence**: SR attributes to the containing bundle (capitalized), Accessibility/IM to the bare tool (lowercase) — so verify the label fix renders "Screencap" in **both** panes; `tccutil` cannot target a bundle-id-less subject; embed must sign inner-to-outer with the team identity + hardened runtime + `screencap-cli.entitlements`.
- `docs/solutions/runtime-errors/macos-tcc-per-process-cache-quit-and-relaunch.md` + `frozen-daemon-cache-immune-tcc-read-via-mp-spawn.md` — post-grant, the long-lived daemon reads stale state; the live read must be a fresh `multiprocessing.spawn` child (never in-process Quartz, never `[sys.executable, "-c"]` — SCR-69); for the app, prefer `launchctl kickstart -k gui/$uid/com.screencap.daemon`.
- `docs/solutions/workflow-issues/macos-shipped-vs-dev-build-confusion.md` — multiple same-named `Screencap.app` copies confuse "which build is live"; a nested same-name rename adds a new Spotlight/`mdfind` collision — re-check `script/clean_dev_macos_state.sh`.

### External References

- Nested same-name `.app` is legal and signable/notarizable: sign **inside-out, never `--deep`** (Quinn, [thread/128166](https://developer.apple.com/forums/thread/128166), [thread/721094](https://developer.apple.com/forums/thread/721094)); notarize the **outermost** container only; the `.app` filename does not appear in the DR.
- Display-name precedence is **`CFBundleDisplayName` → `CFBundleName` → `.app` filename** (filename is the *last-resort fallback*) ([CFBundleName](https://developer.apple.com/documentation/bundleresources/information-property-list/cfbundlename), [QA1544](https://developer.apple.com/library/archive/qa/qa1544/_index.html)) — so the on-device "filename wins" observation is **undocumented** (likely a LaunchServices/TCC cache) and must be A/B-proven before committing to a rename. Localized `InfoPlist.strings` is the Apple-blessed display-name lever.
- TCC grants survive a filename rename when bundle-id + Team are stable ([Quinn thread/702351](https://developer.apple.com/forums/thread/702351), [Rainforest TCC.db deep dive](https://www.rainforestqa.com/blog/macos-tcc-db-deep-dive)).
- SR/IM prompts only fire for code in the **GUI login session** — an `SMAppService.agent` qualifies; a root LaunchDaemon does not ([Quinn thread/128641](https://developer.apple.com/forums/thread/128641)).
- `tccutil reset <Service> <bundle-id>` is per-bundle-id; service constants are `ScreenCapture` / `Accessibility` (not "ScreenRecording") ([ss64](https://ss64.com/mac/tccutil.html)).

---

## Key Technical Decisions

- **Prove the label lever before any rename (spike-first).** Apple docs + the team's own treadmill doc make the filename the *fallback*, not the primary lever; the on-device "filename wins" result was observed on a **TCC-polluted dev machine**, conflicts with Apple's documented `CFBundleDisplayName` precedence (QA1544), and is therefore unverified — the origin doc's "verified this session" should be read as "observed, needs confound-free confirmation." U1 settles it cheaply on a clean machine; U2 applies whichever lever wins. Keeps "Screencap" as the target either way.
- **Order the label levers low-risk-first: plist keys → localized `InfoPlist.strings` → filename rename.** `InfoPlist.strings` is the Apple-blessed display-name override and must be tested *ahead of* the high-risk nested same-name rename, not as an afterthought. The rename branch is only justified if both plist keys and `InfoPlist.strings` fail to move the label on a clean machine.
- **Test the label per pane, not globally.** The treadmill doc documents per-pane label divergence on the *bare-tool* identity (Screen Recording attributes to the containing bundle; Accessibility to the bare tool). The SCR-196 helper-bundle migration *may* unify this, but U1 must confirm "Screencap" renders in **both** SR and Accessibility on a clean machine before exiting, and U5's copy must be able to name a different string per pane if they diverge.
- **R7 detection is a registration-outcome + grant-state-timeout heuristic, not true row-presence.** There is **no public, non-SIP API** that distinguishes an absent TCC row from a present-but-OFF one (both read `denied`/not-granted; TCC.db is SIP-protected — confirmed on-device this session). So R7's "row failed to appear" is detected by: registration call outcome + the grant never resolving to granted within a bounded window after the user has been directed to the pane — **not** by reading row presence. A U6 prerequisite spike confirms whether *any* daemon-accessible presence proxy exists; if none, R7 degrades to this heuristic and AE3 is adjusted accordingly.
- **Proactive registration is daemon-side and additive.** Fired from the daemon's own launchd GUI-session process (responsible-process attribution → the row is the daemon's, satisfying R6), in addition to the retained on-demand path. Hook **both** install paths (Swift notification + Python `launchagent.install`).
- **Decoy removal is identity-scoped.** `tccutil reset All screencap` for the orphan; per-bundle-id reset for the app's SR/Accessibility rows; **never** an untargeted `tccutil reset <service>` (it wipes every app, including `claude`). Never touch `com.screencap.daemon`'s own grants.
- **R7 needs a new "row exists" signal**, distinct from the grant probe (which reports granted/denied/indeterminate, not presence). It must distinguish *absent* from *present-but-OFF* so a correctly-registered-but-not-yet-granted install does not false-trip the block-with-Retry.
- **Upgrade leans on existing reconciliation.** Launchd Label, SMAppService `plistName`, and the BundleProgram launcher path are rename-immune; bootout + version reconciliation (SCR-121/135) rebinds the new-path helper. Verify whether SMAppService re-flags Login-Items approval on a bundle-filename change.

---

## Open Questions

### Resolved During Planning

- *Does the nested same-name bundle sign/notarize?* — Mechanically yes (inside-out, never `--deep`; notarize outer only; DR is filename-independent). The undocumented edge is LaunchServices same-name registration, gated by `sfltool dumpbtm` + `lsregister -dump` checks in U2/U7.
- *Does a rename re-orphan grants?* — No; grants key on bundle-id + Team (R9 holds), provided the re-signed bundle still satisfies its own DR.
- *Which registration APIs populate the rows?* — `CGRequestScreenCaptureAccess()` (SR) + `AXIsProcessTrustedWithOptions({prompt:true})` (Accessibility), daemon-side under launchd. IM excluded.
- *Decoy-removal primitive?* — `tccutil reset All screencap` (orphan) + per-bundle-id reset (app rows); identity-scoped.

### Deferred to Implementation

- **[Affects R3, U1][Needs on-device]** Which lever controls the row label on macOS 26.x — plist keys, localized `InfoPlist.strings`, or the `.app` filename — tested low-risk-first and **per pane** (SR vs Accessibility may diverge). Requires a signed build on a clean machine. U1 also resolves the no-lever-works degrade branch.
- **[Affects R7, U6][Needs on-device]** Whether *any* daemon-accessible, non-SIP signal distinguishes an absent TCC row from a present-but-OFF one (the U6 prerequisite spike). Expected answer: no — hence R7's grant-state-timeout heuristic. Do not read TCC.db directly.
- **[Affects R5/R6, U4][Needs on-device]** Whether **shipped** `com.screencap.macos` has a code path that creates its SR/Accessibility rows (→ R6 needs the call gated, not just reset), or whether those rows only ever existed on dev machines (→ app-row reset is a no-op on shipped builds).
- **[Affects R9, U2/U7][Needs on-device]** Whether `SMAppService` re-prompts for Login-Items approval when the embedded helper's `.app` filename changes (migration doc anticipates a possible re-approval) — and whether a one-time prompt is acceptable or a blocker for the rename branch.

---

## Implementation Units

### U1. Label-lever spike — determine how the row label is controlled on macOS 26.x

**Goal:** Settle whether the Privacy-pane row reads "Screencap" via plist display-name keys (+ LaunchServices/TCC cache refresh) with **no rename**, or only via the `.app` filename — so U2 applies the lowest-risk lever.

**Requirements:** R3

**Dependencies:** None

**Files:**
- Modify (spike branch A): `pyinstaller/screencap.spec` (set `CFBundleName`/`CFBundleDisplayName='Screencap'`; optionally add a localized `InfoPlist.strings`)
- Reference (validation steps): `docs/research/2026-06-30-daemon-helper-bundle-ondevice-validation.md`

**Approach:**
- On a clean-state machine, install a signed (Developer-ID) helper under launchd, then A/B/C the levers **low-risk-first**, reading the label in **both** the SR and Accessibility panes after each:
  - (A) **Plist keys (no rename):** `CFBundleName`/`CFBundleDisplayName='Screencap'`; verify present on the *shipped* Info.plist (`PlistBuddy`); `lsregister -f <helper>`; `tccutil reset All com.screencap.daemon`; re-register; read labels.
  - (B) **Localized `InfoPlist.strings` (no rename):** add `CFBundleDisplayName = "Screencap";` under `Contents/Resources/<lang>.lproj/InfoPlist.strings`; refresh + re-check. (Apple-blessed override — try before any rename.)
  - (C) **Filename rename:** only if A and B both fail — rename the bundle filename and re-check.
- Record which lever moved the label **per pane**; the SR and Accessibility panes may derive the label differently (treadmill doc), so a lever "wins" only if it moves **both**.

**Execution note:** Spike — on a clean-state machine under launchd, never shell-run (confound). Not unit-testable; output is a documented decision that selects U2's branch.

**Patterns to follow:** The U8 runbook's confound-free method; `tccutil`/`lsregister` usage from the registration spike.

**Test scenarios:**
- Test expectation: none — investigative spike; the deliverable is the lever decision recorded in the plan/runbook, validated by U7.

**Verification:**
- A written determination naming the winning lever (`plist keys` | `InfoPlist.strings` | `filename rename`), confirmed to render "Screencap" in **both** SR and Accessibility panes on a clean machine.
- **No-lever-works exit branch:** if no lever reliably yields "Screencap" in both panes on a clean machine, U1 records that outcome and the plan **degrades R3 to "name + icon the actual rendered string" per pane** (U5 names whatever macOS shows; R4 icon-match still holds). R2/R5/R6/R7 (no manual-add, single row, daemon-only attribution, block-with-Retry) are unaffected by this degradation — only the exact "Screencap" string goal relaxes.

---

### U2. Apply the label fix (and, if rename, update all path references + signing/LS verification)

**Goal:** Make the row render "Screencap" via the lever U1 selected, preserving the daemon's bundle id, DR, and existing grants.

**Requirements:** R3, R9

**Dependencies:** U1

**Files:**
- **Plist-keys branch (no rename):** `pyinstaller/screencap.spec` (`CFBundleName`/`CFBundleDisplayName='Screencap'`); add a cache-refresh step (`lsregister -f`) to the install path — `macos/Screencap/Scripts/embed-cli.sh` and/or `src/screencap/daemon/launchagent.py`.
- **Rename branch (same-name nested `Screencap.app`) — required:** `pyinstaller/screencap.spec` (`name='Screencap.app'`); `macos/Screencap/Scripts/embed-cli.sh` (L24/L27/L29 + the literal `<<'EOF'` launcher heredocs L92/L101 + warnings); `macos/Screencap/Controllers/CLIClient.swift` (`resolveBinary` + comments); `script/sign_app.sh` (L66-67); `tests/daemon/test_launchagent.py` (L135-138 exact-string).
- **Rename branch — developer-ergonomics (not a gate on R1–R9):** `script/clean_dev_macos_state.sh` (disambiguate the new nested `Screencap.app` in Spotlight/`mdfind`).
- Both branches: a `codesign -d -r-` **CI gate** comparing the helper's DR against a committed golden string (extend `sign_app.sh`'s L143 bundle-id gate to the full DR), so a signing regression that silently changes the DR — and orphans every user's grant on upgrade — fails the build, not just the manual runbook.

**Approach:**
- Plist-keys branch is strongly preferred (no nesting risk). Rename branch only if U1 proved the filename is the lever.
- Rename branch: keep `bundle_identifier='com.screencap.daemon'` and `CFBundleExecutable='screencap'`; sign **inside-out, never `--deep`**; notarize the **outer** app only; then verify the nested same-name layout with `codesign --verify --deep --strict`, `spctl -a -vv`, `sfltool dumpbtm`, and `lsregister -dump | grep -i screencap` (no shadowing; helper registers as `com.screencap.daemon` at its nested path).
- Both branches: confirm `codesign -d -r-` on the helper is byte-identical before/after (DR unchanged → grants preserved, R9).

**Execution note:** Branch selection is gated by U1; do not implement both branches — implement the one U1 selects.

**Patterns to follow:** `embed-cli.sh` inside-out signing order; `sign_app.sh` DR gate (L143); the migration doc's `codesign -d -r-` invariant.

**Test scenarios:**
- Happy path (rename branch): `tests/daemon/test_launchagent.py` launcher exact-string updated to the new path and passing.
- Edge case (rename branch): build emits the helper at the new path; `embed-cli.sh` embed + sign succeeds with no `--deep` in the signing calls.
- Error path: signing gate fails loudly if the helper DR does not name `com.screencap.daemon` (existing gate still fires).
- Integration (on-device, U7): `codesign --verify --deep --strict` + `spctl -a -vv` pass on the assembled `.app`; `codesign -d -r-` DR is byte-identical to pre-change.

**Verification:**
- The shipped helper's row reads "Screencap" in SR + Accessibility on a clean machine (U7), bundle id stays `com.screencap.daemon`, and a pre-existing grant survives the change.

---

### U3. Proactive install-time TCC registration (daemon-side, both install paths)

**Goal:** The daemon self-registers its Screen Recording + Accessibility rows at install/approval so a pre-populated row always exists before the user reaches Settings — additive to the retained on-demand path.

**Requirements:** R1, R2, R6

**Dependencies:** None (independent of the label branch)

**Files:**
- Modify: `src/screencap/daemon/launchagent.py` (headless install → trigger registration after the daemon is up)
- Modify: `macos/Screencap/Controllers/DaemonInstallController.swift` (on `screenCapDaemonInstalledAndRunning`, trigger SR + Accessibility registration) and/or `macos/Screencap/Controllers/PermissionController.swift`
- Reference/Modify: `src/screencap/daemon/permission_register.py`, `src/screencap/daemon/app.py` (`/v0/permission.request`)
- Test: `tests/daemon/test_permission_request_verb.py`, `tests/daemon/test_launchagent.py`, `macos/ScreencapTests/DaemonInstallControllerTests.swift`

**Approach:**
- Registration runs **inside the daemon's own launchd GUI-session process** (responsible-process attribution → the row is the daemon's, satisfying R6). Both triggers must reach the daemon, never call `register_permission` in the triggering process:
  - Swift: on the install hook, call the `/v0/permission.request` daemon verb for `screen_recording` + `accessibility`.
  - Python headless (`launchagent.install()` success): **round-trip through `/v0/permission.request` over the socket after the daemon is confirmed up** — a direct in-process `register_permission()` call in the CLI would attribute the rows to the CLI's responsible process (e.g. Terminal), violating R6. (Headless/SSH installs may lack a GUI session and silently no-op the row; that lands in R7's recoverable Retry state — see U6.)
- **First-run / once-per-version gate (do not fire on every reconnect).** The `screenCapDaemonInstalledAndRunning` notification posts on *every* successful daemon poll (app launch, reconnect, version reconciliation), not just first install. Gate proactive registration behind a first-install/once-per-version flag (reuse the existing `registrationAttemptedKey` UserDefaults one-shot pattern in `DaemonInstallController`) so it doesn't re-fire repeatedly.
- **Ordering invariant:** the walkthrough must not present the "open the pane and toggle" step until install-time registration has been confirmed fired, so the user never reaches an empty pane (the manual-add trigger).
- Keep the on-demand `/v0/permission.request` path intact as a fallback (do not remove). Make install-time registration idempotent (safe to re-fire).
- Do **not** register Input Monitoring.

**Execution note:** The actual row appearing is GUI-session + launchd behavior — verify on-device in U7; unit tests cover the trigger/wiring, not the OS row.

**Patterns to follow:** existing `permission.request` verb flow; the `screenCapDaemonInstalledAndRunning` notification consumer pattern.

**Test scenarios:**
- Happy path: after a simulated successful install, the registration trigger calls `register_permission` for exactly `screen_recording` and `accessibility` (not `input_monitoring`).
- Edge case: re-firing the install hook does not error and does not double-prompt (idempotent).
- Error path: a registration failure (mechanism returns False / raises) is swallowed and does not fail the install (best-effort), and leaves the on-demand path available.
- Edge case (gate): a second `screenCapDaemonInstalledAndRunning` post (reconnect/relaunch) within the same install/version does NOT re-fire registration.
- Integration: the headless path triggers registration via the `/v0/permission.request` daemon verb (asserted), never via an in-process `register_permission` call in the CLI process (attribution guard).
- Covers AE1 (the pre-populated row that AE1 depends on originates here).

**Verification:**
- On a clean machine (U7), opening the SR/Accessibility panes right after install shows a pre-populated "Screencap" row with no "+" add; both install paths behave identically.

---

### U4. Identity-scoped decoy/orphan TCC cleanup at install

**Goal:** Leave exactly one "Screencap" row per pane by removing the orphaned old-identity row and any legacy app-identity rows — without touching the daemon's own grants or the app's microphone grant. (Also structurally satisfies R8: with exactly one correct row and no decoys, the revoke path is unambiguous — the user cannot toggle the wrong row off.)

**Requirements:** R5, R6, R8

**Dependencies:** None

**Files:**
- Modify: `src/screencap/daemon/launchagent.py` or a new daemon-side cleanup helper invoked at install (and/or `macos/Screencap/Controllers/DaemonInstallController.swift` post-install)
- Test: new `tests/daemon/test_tcc_cleanup.py`

**Approach:**
- Run `tccutil reset All screencap` (the old **bare** orphan identity — `All` is correct here because that identity no longer ships) once at install. For the app's legacy SR/Accessibility rows, use a **per-bundle-id, per-service** reset (`ScreenCapture`, `Accessibility`) scoped to `com.screencap.macos`.
- **Security guard (the cleanup is a destructive surface — a wrong scope wipes unrelated apps' grants):** encode named allowlists and assert the exact built argv in tests:
  - `ALLOWED_ALL_RESET_IDENTITIES = {"screencap"}` — `All` may ONLY pair with the bare orphan, never with `com.screencap.macos` (would wipe its **Microphone** grant) and never with `com.screencap.daemon` (would wipe the daemon's real grants).
  - `ALLOWED_SERVICES_FOR_APP = {"ScreenCapture", "Accessibility"}` — the only services ever reset for `com.screencap.macos`; never `All`, never microphone/`ListenEvent`.
  - Never emit a bare `tccutil reset <service>` with no bundle id (wipes all apps).
- **First-run / once-per-version gate:** invoke at first install/version only — NOT on every `screenCapDaemonInstalledAndRunning` post (which fires on every reconnect), so the resets don't repeatedly clear a row the user may be mid-interaction with. Reuse U3's gate.
- **App-row recreation:** `tccutil reset` clears the stored row but does not stop it being recreated if app code touches SR/Accessibility APIs. As part of U4 (or U1's clean-machine pass), determine whether **shipped** `com.screencap.macos` has any code path that creates these rows; if so, R6 requires removing/gating that call, not just resetting (add the file/step). If the rows only ever existed on dev machines, document that the app-row reset is a no-op on shipped builds.
- Idempotent and best-effort (a failed reset must not fail install).

**Execution note:** Treat the command set as the testable surface — assert the exact `tccutil` argv built against the allowlists, since the danger is an over-broad reset.

**Patterns to follow:** the migration doc's prescribed `tccutil reset All screencap`; daemon best-effort/fail-soft conventions.

**Test scenarios:**
- Happy path: cleanup builds `tccutil reset All screencap` + per-service resets for `com.screencap.macos` against `ScreenCapture` + `Accessibility` only.
- Error path (P1 guard): the builder NEVER emits `tccutil reset All com.screencap.macos` (would wipe Microphone) or any reset targeting `com.screencap.daemon`; `All` only ever pairs with `screencap`.
- Edge case (guard): never a bare `tccutil reset ScreenCapture`/`reset Accessibility` with no bundle id.
- Error path: a non-zero `tccutil` exit (e.g. "No such bundle identifier") is tolerated and does not fail install.
- Covers AE2.

**Verification:**
- On a clean/representative machine (U7), after install each pane shows exactly one "Screencap" row; the orphan `screencap` and app rows are gone; the daemon's grants AND the app's microphone grant are intact; the app row does NOT reappear after an app relaunch + normal use.

---

### U5. Onboarding copy → "Screencap" + helper icon in the walkthrough

**Goal:** The walkthrough names the exact row ("Screencap") and shows the helper's icon so the user matches it unambiguously.

**Requirements:** R3, R4

**Dependencies:** U1 (final display string), but copy work can proceed against the "Screencap" target

**Files:**
- Modify: `macos/Screencap/Controllers/PermissionController.swift` (`PrivacyPane.helperSettingsEntryName` → "Screencap" for SR/Accessibility)
- Modify: `macos/Screencap/Views/Privacy/FirstRunPermissionsView.swift` — three sites, all in lockstep: the hardcoded row title `Text("\(pane.displayName) for Screencap helper")` (~L324), the `daemonRationale` copy, and the now-dead disambiguation conditional `entry == "Screencap Helper" ? …` (~L385-388); plus render the helper icon in `daemonPermissionRow`
- Modify: `pyinstaller/screencap.spec` (set the helper bundle `icon=` so it has a real icon) and add/locate the icon asset
- Test: `macos/ScreencapTests/PermissionControllerTests.swift`

**Approach:**
- Replace "Screencap Helper" with "Screencap" in **all three** copy sites above (the row title literal would otherwise contradict the rationale body within the same screen), and delete the disambiguation conditional (its true branch is unreachable once the entry name is "Screencap").
- If U1 selected the no-lever-works degrade branch, this copy names the **actual rendered string per pane** instead of the literal "Screencap".
- **Icon decision (product):** the helper icon should be the **same mark as the main app icon** (so the System Settings row and walkthrough read as "Screencap") — confirm this vs. a distinct variant. Specify the asset source for `screencap.spec icon=` and the walkthrough `Image()`, **and a SF Symbol fallback** so an absent/failed asset never renders a blank frame.
- **Revoke surface (R8):** a post-revoke row is *present-but-OFF*, not absent — it must render the normal "Grant" state, NOT U6's block-with-Retry. Confirm the existing `clearSetupDismissed`/`shouldShowFinishSetupBanner` re-arm is the intended recovery surface when a required grant is revoked.
- **Upgrade surface (R9):** when grants already carry over on upgrade, the walkthrough should suppress itself (rows read Granted from the daemon probe, ignoring transient label state) rather than re-prompt — confirm this is the `updateDaemonGrants` auto-suppress path.

**Patterns to follow:** existing `daemonPermissionRow` rendering; the app's icon asset pipeline; the `setupDismissed`/`shouldShowFinishSetupBanner` re-arm logic.

**Test scenarios:**
- Happy path: `PrivacyPane.helperSettingsEntryName` returns "Screencap" for `.screenRecording` and `.accessibility`; the row-title and rationale strings agree (no within-screen "helper" vs "Screencap" mismatch).
- Edge case: microphone naming is unaffected; Input Monitoring is not surfaced.
- Edge case (revoke): a present-but-OFF row shows the normal Grant state, not block-with-Retry.
- Test expectation (icon rendering): none for the asset itself — visual; the SF Symbol fallback is unit-assertable (no asset → fallback symbol, never blank).

**Verification:**
- Walkthrough copy names "Screencap" exactly (or the per-pane rendered string under the degrade branch), all copy sites agree, and shows the helper icon (or fallback); matches the System Settings row label/icon on a clean machine (U7).

---

### U6. "Row failed to appear" detection + block-with-Retry walkthrough state

**Goal:** When setup can't be completed (registration didn't take / the row never became grantable), the walkthrough blocks with a clear state + Retry — never sends the user to manual-add, never silently proceeds.

**Requirements:** R7

**Dependencies:** U3 (registration must run first)

**Files:**
- Modify (if a presence proxy exists): a daemon-side check + a **read-only** verb in `src/screencap/daemon/app.py`
- Modify: `macos/Screencap/Controllers/PermissionController.swift` (per-pane row-state machine: in-flight → granted | needs-action | block-with-Retry; bounded retry)
- Modify: `macos/Screencap/Views/Privacy/FirstRunPermissionsView.swift` (render the in-flight and block-with-Retry states on the permission row)
- Test: `tests/daemon/test_*` for any new check; `macos/ScreencapTests/PermissionControllerTests.swift` for the state machine

**Approach:**
- **Prerequisite spike (do first):** there is **no public, non-SIP macOS API for TCC row presence** — both an absent row and a present-but-OFF row read `denied`/not-granted, and TCC.db is SIP-protected (confirmed on-device this session). Spike whether *any* daemon-accessible, non-SIP proxy distinguishes them (e.g. the request API's own return on a second call, a no-op-vs-prompt signal). **Do not read TCC.db directly** (SIP-protected, fragile across OS versions, would silently return indeterminate). Record the outcome.
- **Detection = registration-outcome + grant-state-timeout heuristic** (the fallback when no presence proxy exists, which is the expected case): registration was fired (U3) AND the grant has not resolved to `granted` within a bounded window *after the user has been directed to the pane*. This conflates "absent" with "present-but-not-yet-toggled," so the block must **not** fire until the user has had a real chance to toggle — gate it on "user reached the toggle step + elapsed budget," not on an immediate post-install read. Treat `indeterminate` as "couldn't verify → keep Retry available, don't hard-block."
- Drive the walkthrough state: granted → advance; not-yet (within budget) → normal Grant/in-flight state; budget exhausted with no grant → block-with-Retry (re-fires U3 registration). Never open a manual-add flow; never auto-advance past a required-but-ungranted permission.
- **In-flight state:** define what the row shows in the window between "install registration fired" and "result known" — reuse the existing `isDaemonRegistering` spinner, not the block state (the block state is only after the budget is exhausted).
- **Retry bound + escape:** specify a max retry / total time budget; after it, show an escalated "still couldn't set this up — see help" state. Reconcile with the always-present "Skip for now" footer button: "never silently proceeds" means never *auto*-advance — the user may still explicitly Skip, but the required grant stays unsatisfied and the start-block (Screen Recording) and recovery banner remain.
- **If a presence verb is added:** it is **read-only — not audited** (consistent with `permission_probe` and the query verbs) and **deliberately NOT in `_ACTIVITY_PATHS`**, so the walkthrough's retry polling can't pin an auto-spawned daemon open (per the SECURITY.md diagnostic-verb rationale).
- **Copy:** the install-step `installFailureCopy` strings name helper-launch failures and are wrong here — specify row-absent copy, e.g. headline "Screen Recording couldn't be set up" / body "Screencap tried to register this permission but it didn't appear in System Settings — macOS may still be loading. Tap Retry to try again." (final wording is the user's call).

**Execution note:** Start with the prerequisite spike, then the Swift state-machine test (in-flight → budget-exhausted → block → Retry → recover); the live behavior is verified on-device in U7.

**Patterns to follow:** `DaemonInstallController` `installFailed`/Retry + `installFailureCopy`; `FirstRunPermissionsView` install-step Retry; `isDaemonRegistering` in-flight spinner; the query-verb (non-audited, non-activity) pattern.

**Test scenarios:**
- Happy path: grant resolves within budget → advance, no block.
- Edge case: present-but-OFF / not-yet-toggled within budget → normal Grant/in-flight state, NOT block.
- Error path: budget exhausted, no grant → block-with-Retry; Retry re-fires registration; no manual-add offered.
- Edge case: indeterminate → Retry available, not a hard false-block.
- Edge case: retry budget exhausted → escalated state, "Skip for now" still available but required grant stays unsatisfied.
- Covers AE3.

**Verification:**
- With registration deliberately suppressed on-device (U7), the walkthrough reaches block-with-Retry (not a "+" add) after the budget and recovers when Retry succeeds; a normal install never false-blocks before the user has had a chance to toggle.

---

### U7. Clean-machine validation gate (extend the U8 runbook)

**Goal:** Prove the whole foolproof flow on a clean machine — the only place these behaviors are verifiable (this dev machine's TCC is polluted).

**Requirements:** R1–R9

**Dependencies:** U2, U3, U4, U5, U6

**Files:**
- Modify: `docs/research/2026-06-30-daemon-helper-bundle-ondevice-validation.md` (add steps for: row reads "Screencap" in SR + Accessibility; row pre-populated at install with no "+"; exactly one row / decoys cleared; block-with-Retry on suppressed registration; grant survives upgrade rename)

**Approach:**
- Extend the existing confound-free runbook (launchd, quarantined/translocated download, clean state). Add per-leg PASS/FAIL checks for each of R1–R9.
- **Label leg:** confirm the row reads "Screencap" (or the U1-degrade rendered string) in **both** SR and Accessibility panes — per pane, since they may derive the label differently.
- **Pre-population leg:** open the panes right after install → a "Screencap" row is already present (no "+").
- **Registration-race leg:** open Settings *before* registration completes, and have the pane already open when the row registers (the SR pane doesn't live-refresh) → confirm the documented quit/reopen-Settings recovery and that R7's Retry handles the transient absent state without dead-ending.
- **Headless-install leg:** install via `screencap setup` / `serve --install` from an SSH session (no GUI) → confirm the row appears in the GUI session's pane, or that R7's Retry recovers gracefully; confirm the grant attributes to `com.screencap.daemon`, not the installing shell.
- **Decoy leg:** exactly one "Screencap" row per pane; orphan + app rows gone; daemon grants AND app microphone grant intact; **relaunch the app + exercise normal flows, confirm the app row does NOT reappear**.
- **Upgrade leg:** install an older-identity build, upgrade → confirm relabel + no re-grant (R9), decoys cleared, and — if the rename branch was taken — whether `SMAppService` re-prompts for Login-Items approval (a PASS/FAIL distinct from the TCC-grant-survival check; decide acceptable = one-time prompt with guiding copy, or a blocker).
- **Nested-bundle leg (rename branch only):** `codesign --verify --deep --strict`, `spctl -a -vv`, `sfltool dumpbtm`, `lsregister -dump | grep -i screencap` (no name shadowing; helper registers as `com.screencap.daemon` at its nested path); DR golden-file gate matches.

**Execution note:** Manual on-device acceptance gate; not unit-testable. This is the merge/release gate.

**Patterns to follow:** the existing U8 runbook structure and verdict matrix.

**Test scenarios:**
- Test expectation: none (manual runbook) — but the runbook itself must enumerate a pass/fail check for each of R1–R9.

**Verification:**
- A completed runbook pass on a clean machine with every R1–R9 check marked PASS (or an explicit, escalated exception).

---

## System-Wide Impact

- **Interaction graph:** Two install paths (Swift `SMAppService` + Python `launchagent.install`) both gain registration (U3) and cleanup (U4) hooks; the `screenCapDaemonInstalledAndRunning` notification gains a new consumer; the walkthrough gains a new per-row state (U6).
- **Error propagation:** Registration and cleanup are best-effort/fail-soft and must never fail an otherwise-successful install; the only hard block is U6's "required row absent."
- **State lifecycle risks:** Freshly-registered rows default OFF and the SR pane doesn't live-refresh — U6 must not conflate "present-but-OFF" with "absent"; post-grant the daemon must re-read via fresh-spawn/kickstart, not in-process Quartz.
- **API surface parity:** `tccutil` cleanup identities/services are a security-sensitive surface — an over-broad reset would wipe unrelated apps; constrain + assert in tests (U4).
- **Unchanged invariants:** Bundle id `com.screencap.daemon`, launchd Label, SMAppService `plistName`, and the `BundleProgram` launcher path are all unchanged; the on-demand `permission.request` path is retained; Input Monitoring stays optional/out.

---

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| The "filename wins" label behavior is undocumented; a rename may be unnecessary or insufficient | U1 spike proves the lever (plist → `InfoPlist.strings` → rename, low-risk-first) before U2 commits |
| **No lever yields "Screencap" on a clean machine** (third outcome) | U1 no-lever-works exit branch: degrade R3 to "name + icon the actual rendered string per pane"; R2/R5/R6/R7 unaffected |
| **SR and Accessibility panes derive the label differently** ("Screencap" in one, not the other) | U1 tests + records the label per pane; U5 copy can name a different string per pane; U1 doesn't exit "resolved" unless both panes read "Screencap" |
| **R7 has no row-presence API** (absent vs present-but-OFF both read `denied`; TCC.db SIP-protected) | U6 prerequisite spike; re-scoped to a registration-outcome + grant-state-timeout heuristic that never reads TCC.db and never false-blocks before the user has had a chance to toggle |
| Nested same-name `Screencap.app` trips LaunchServices/`open`-by-name or `codesign --verify --strict` | Only taken if U1 forces it; `sfltool dumpbtm` + `lsregister -dump` + `--verify --deep --strict` + `spctl` checks (U2/U7); references resolve by full path/bundle-id, not name |
| **Over-broad `tccutil reset` wipes unrelated apps' grants — incl. the app's own Microphone** | U4 `ALLOWED_ALL_RESET_IDENTITIES={screencap}` + `ALLOWED_SERVICES_FOR_APP={ScreenCapture,Accessibility}`; never `reset All com.screencap.macos`, never target `com.screencap.daemon`; exact argv asserted in tests |
| **App's SR/Accessibility row reappears after reset** (app code re-touches the API) | U4 finds + gates the shipped app code path that creates it (not just reset); U7 relaunch-and-reuse leg confirms it stays gone |
| Registration attributed to the app/CLI, not the daemon (breaks R6) | All registration fired daemon-side via `/v0/permission.request`; headless path round-trips the verb, never in-process (U3) |
| **U3/U4 re-fire on every reconnect** (`screenCapDaemonInstalledAndRunning` posts on every poll) | First-install/once-per-version gate (reuse `registrationAttemptedKey`) on both hooks |
| SMAppService re-prompts for Login-Items approval on filename change → daemon can't run → R1/R9 break on upgrade | U7 explicit PASS/FAIL leg; decide acceptable (one-time prompt + guiding copy) vs blocker before shipping the rename branch |
| Upgrade re-orphans grants | Bundle id + Team unchanged → DR unchanged → grants preserved; `codesign -d -r-` **CI golden-file gate** (U2) catches a silent DR regression |

---

## Documentation / Operational Notes

- Update `docs/solutions/build-errors/daemon-tcc-identity-migration-2026-06-30.md` and any helper-path references if U2 takes the rename branch.
- Close/fold **SCR-199** into this work (U4 absorbs its orphaned-row cleanup).
- The clean-machine U8 runbook pass (U7) is the release gate; do not ship on dev-machine results.

---

## Sources & References

- **Origin document:** [docs/brainstorms/2026-06-30-foolproof-macos-permission-onboarding-requirements.md](docs/brainstorms/2026-06-30-foolproof-macos-permission-onboarding-requirements.md)
- Prior onboarding decisions: `docs/brainstorms/2026-06-05-daemon-tcc-permission-visibility-onboarding-requirements.md`, `docs/plans/2026-06-30-002-feat-daemon-helper-bundle-tcc-plan.md`
- Learnings: `docs/solutions/build-errors/daemon-tcc-identity-migration-2026-06-30.md`, `docs/research/2026-06-05-daemon-tcc-registration-spike.md`, `docs/solutions/build-errors/macos-ad-hoc-signing-tcc-rebuild-treadmill.md`, `docs/solutions/runtime-errors/macos-tcc-per-process-cache-quit-and-relaunch.md`
- Validation gate: `docs/research/2026-06-30-daemon-helper-bundle-ondevice-validation.md`
- Related PR: Input Monitoring demotion — screencap#315
- Related ticket: SCR-199 (absorbed)
