---
title: "feat: Daemon helper-bundle TCC registration (SCR-196)"
type: feat
status: active
date: 2026-06-30
origin: docs/brainstorms/2026-06-30-daemon-helper-bundle-tcc-registration-requirements.md
---

# feat: Daemon helper-bundle TCC registration (SCR-196)

## Summary

Repackage the recording daemon from a bare nested Mach-O into a Developer-ID-signed helper `.app` (bundle id `com.screencap.daemon`) by extending the PyInstaller spec to emit a bundle, embedding it into the outer app, re-pointing the SMAppService launch **agent** at the executable inside it, extending inside-out signing/notarization to cover it, and updating binary-path resolution. The retained self-registration becomes the GUI-session trigger that makes TCC prompts fire; a System Settings deep-link stays the standing fallback.

---

## Problem Frame

The daemon ships as a bare Mach-O (`Contents/Resources/screencap/screencap`, code-signing identifier `screencap`, no `Info.plist`, no bundle id). macOS reserves TCC auto-population and `tccutil` bundle-id targeting for app bundles / properly-placed helpers with a responsible-code ancestor, so first-run onboarding lands users on empty Privacy panes and `tccutil reset … screencap` / `… com.screencap.daemon` both fail. Full situational detail and the live-confirmed artifact facts are in the origin doc (see Sources & References).

---

## Requirements

- R1. Daemon runs as a TCC subject with a stable bundle identifier (`com.screencap.daemon`), Developer-ID signed, with a responsible-code ancestor — a real helper bundle, not a bare nested Mach-O. (origin R1)
- R2. During onboarding, each required permission's Privacy pane shows a real, toggleable entry for the helper — no manual `+`/deep-binary-path add. (origin R2)
- R3. The helper identity is `tccutil`-targetable by bundle id. (origin R3)
- R4. Screen Recording, Accessibility, and Input Monitoring each end with a populated, toggleable native entry; Input Monitoring is in scope, not degraded to manual-only. (origin R4)
- R5. The existing self-registration path is retained as the GUI-session prompt trigger / transitional fallback, not removed. (origin R5)
- R6. Shipped as a deliberate, communicated one-time re-grant release; stale prior-identity daemon + socket cleared so the new helper registers cleanly. (origin R6)
- R7. Daemon continues to install/launch via SMAppService/launchd (agent); existing install flow keeps working against the new bundle. (origin R7)
- R8. On-device validation against the shipped Developer-ID + hardened-runtime identity, clean machine state, confirms native registration for all three permissions and post-grant recording. (origin R8)

**Origin actors:** A1 (new user, first run), A2 (existing tester, re-grant cohort), A3 (macOS TCC + System Settings)
**Origin flows:** F1 (first-run grant), F2 (one-time re-grant)
**Origin acceptance examples:** AE1 (covers R2, R4), AE2 (covers R3), AE3 (covers R5), AE4 (covers R6)

---

## Scope Boundaries

- Removing the U8 self-registration path — excluded; retained per the hybrid decision (R5).
- Changing the `disable-library-validation` / hardened-runtime entitlement set — out; research suggests it may be droppable once all nested dylibs are same-team signed, but that is a separate hardening task. Keep current `screencap-cli.entitlements`.
- Org Team-ID flip, auto-update (Sparkle), and CLI-tarball notarization — owned by `docs/plans/2026-06-03-001-feat-macos-app-developer-id-notarized-distribution-plan.md`.
- Permission *model* changes (which permissions are required; app-vs-daemon ownership) — unchanged. Microphone stays app-owned.
- Onboarding walkthrough UX redesign — out; copy may be tightened (U7) but the flow shape is unchanged.
- Dev-machine stale-TCC-row cleanup (the ~74 ad-hoc rows) — handled by `script/clean_dev_macos_state.sh`, not this work.

### Deferred to Follow-Up Work

- **Highest-priority hardening item:** dropping `disable-library-validation` (and reassessing `allow-jit` / `allow-unsigned-executable-memory`) after verifying all PyInstaller dylibs are same-team signed. Now elevated in severity because these entitlements ride on the binary that is the *TCC subject* for Screen Recording / Accessibility / Input Monitoring — code injected into that process could exfiltrate the capture stream without a separate prompt. U4 records the accepted residual in `SECURITY.md`; this ticket removes it. Separate hardening ticket.

---

## Context & Research

### Relevant Code and Patterns

- `pyinstaller/screencap.spec` — produces the `--onedir` `COLLECT` named `screencap` (→ `dist/screencap/`). `EXE(... console=True)` + `COLLECT(...)`. `--onedir` is deliberate: `multiprocessing.spawn` re-execs the binary per child and onefile would re-extract ~100 MB each time (spec L4-5). No `BUNDLE()` step today.
- `macos/Screencap/Scripts/embed-cli.sh` — `ditto`s `dist/screencap/` → `Contents/Resources/screencap/` (L115); writes `Contents/Resources/screencap-daemon-launcher` (Debug dev-source aware / Release exec-only, L16-83); stamps `Contents/Resources/screencap-cli-version` (L125-149); signs inside-out via `sign_embedded_cli()` using `EXPANDED_CODE_SIGN_IDENTITY` + `screencap-cli.entitlements` (L151-193).
- `macos/project.yml` — `preBuildScripts` runs `embed-cli.sh` (L57-61); `postBuildScripts` copies `com.screencap.daemon.plist` → `Contents/Library/LaunchAgents/` (L63-71); `ENABLE_HARDENED_RUNTIME: YES`, `CODE_SIGN_STYLE: Automatic`, `DEVELOPMENT_TEAM: ${DEVELOPMENT_TEAM}`.
- `macos/Screencap/Resources/com.screencap.daemon.plist` — `Label=com.screencap.daemon` (note: launchd label ≠ TCC subject), `BundleProgram=Contents/Resources/screencap-daemon-launcher`, `RunAtLoad`, `ExitTimeOut=30`, `PATH` env only (no `~` keys).
- `src/screencap/daemon/launchagent.py` `render_plist(...)` — generates the plist; `bundle_program` param controls the `BundleProgram` key. Pinned byte-for-byte against the bundled plist by `tests/daemon/test_launchagent.py::test_bundled_macos_launchagent_plist_matches_renderer`.
- `script/sign_app.sh` — authoritative inside-out signing (dylibs → CLI binary w/ `screencap-cli.entitlements` → Frameworks → outer app), `--options runtime --timestamp`, no `--deep`; verifies with `codesign --verify --strict`. `script/notarize_app.sh` — `notarytool` + `stapler`.
- `macos/Screencap/Controllers/CLIClient.swift` `resolveBinary()` (L103-139) — resolves `Bundle.main.resourceURL/screencap/screencap`; precedence: `SCREENCAP_CLI_PATH` env → bundled → Debug dev fallback.
- `macos/Screencap/Controllers/DaemonInstallController.swift` — `SMAppService.agent(plistName: "com.screencap.daemon.plist")`; `requiresApproval` → Login Items path; `bootout` of `gui/<uid>/com.screencap.daemon` + version reconciliation (SCR-121/135); `BundledDaemonVersion.resourceName = "screencap-cli-version"`.
- `src/screencap/daemon/permission_register.py` + `app.py` (`/v0/permission.request`) + `src/screencap/engine/platform/darwin.py` (`request_screen_recording_access` / `request_accessibility_access` / `register_input_monitoring_access`) — the retained self-registration mechanisms.

### Institutional Learnings

- `docs/solutions/build-errors/macos-ad-hoc-signing-tcc-rebuild-treadmill.md` — TCC keys on `(identifier, team)` via the designated requirement; Xcode auto-signing does **not** recurse into `Contents/Resources/`, so nested code must be explicitly re-signed with team identity + hardened runtime + the CPython entitlements.
- `docs/solutions/runtime-errors/macos-tcc-per-process-cache-quit-and-relaunch.md` — TCC state is cached per process at launch; post-grant pickup needs a relaunch (`launchctl kickstart`); a child spawned via `Process.run()` does not inherit the parent .app's TCC identity.
- `docs/solutions/runtime-errors/launchd-plist-tilde-expansion-2026-05-09.md` — launchd does not expand `~`/`$HOME`; bundled plists must omit path-valued keys (already honored).
- `docs/solutions/runtime-errors/frozen-daemon-cache-immune-tcc-read-via-mp-spawn.md` — frozen daemon re-reads live TCC only via a fresh `multiprocessing.spawn` child (relevant to the spawn-inside-.app risk below).
- `docs/research/2026-06-05-daemon-tcc-registration-spike.md` — the original spike, validated only on the **ad-hoc** identity; names bundle relocation + responsible-code ancestor as the real fix.

### External References

- Apple DTS (Quinn) [thread/732291](https://developer.apple.com/forums/thread/732291): install the helper via `SMAppService` so TCC tracks responsibility from helper → app; bare nested execs break attribution.
- Apple DTS (Quinn) [thread/128641](https://developer.apple.com/forums/thread/128641): **Input Monitoring / Screen Recording prompts only fire for code with a bundle running in the GUI login session** — a true daemon (root, no GUI session) never prompts. This is the load-bearing constraint behind keeping an **agent** + the self-registration trigger.
- Apple DTS (Quinn) [thread/698337](https://developer.apple.com/forums/thread/698337): align code-signing identifier with bundle id; helper id as sub-identifier of the app.
- [PyInstaller feature-notes](https://pyinstaller.org/en/stable/feature-notes.html): `BUNDLE` cross-links `Frameworks/`↔`Resources/` so collected dylibs satisfy codesign placement; PyInstaller re-signs nested binaries and enables hardened runtime; avoid `--deep` as the authoritative pass.
- [Rainforest QA TCC.db deep dive](https://www.rainforestqa.com/blog/macos-tcc-db-deep-dive) / Quinn [thread/702351](https://developer.apple.com/forums/thread/702351): TCC stores a `csreq`/designated-requirement per `(service, client)`; changing the identifier orphans prior grants even with the same team → one-time re-grant.

---

## Key Technical Decisions

- **Keep `SMAppService.agent`, but point `BundleProgram` *directly* at the nested helper `.app`'s `Contents/MacOS/screencap` — not at the loose `/bin/sh` launcher.** An agent runs in the GUI session (required for prompts); a root daemon never prompts. The current launcher indirection (`/bin/sh` script in the *outer* app's `Resources` execing the binary) risks defeating the responsible-code-ancestor attribution that is the whole point of R1 — launchd would launch `/bin/sh`, and the TCC subject the spawned process resolves to may not be `com.screencap.daemon`. Drop/relocate the launcher for the Release path so the registered, launched, and TCC-attributed identity are the same bundle. Lowest churn to the working `DaemonInstallController` flow vs. switching to `loginItem` (see Alternatives for the `loginItem` fallback).
- **Validate the TCC *subject identity* of the running daemon AND a spawned capture worker, not just that registration/launch succeeded.** Capture runs in `multiprocessing.spawn` children re-exec'd from `sys.executable`; if a spawned worker's code identity (responsible-process / cdhash) diverges from the granted `com.screencap.daemon` helper, the pane shows the entry toggled ON yet the worker's `CGPreflightScreenCaptureAccess` reads `False` — a "looks granted but records wallpaper" failure. U1's go/no-go gate and U8 must `codesign` the live daemon PID *and* a spawned worker PID and confirm both resolve to `com.screencap.daemon`.
- **Produce the helper `.app` from the PyInstaller spec (`BUNDLE`), not by hand-wrapping in `embed-cli.sh`.** PyInstaller's bundle build does the `Frameworks/`↔`Resources/` cross-linking that satisfies codesign placement rules for the ~340 nested dylibs; hand-rolling that is the documented failure mode. Verify the produced `Info.plist`'s `CFBundleExecutable` matches the actual Mach-O filename (else codesign can't locate the designated executable and the DR won't name `com.screencap.daemon`).
- **Bundle id fixes attribution/listing/`tccutil`/persistence; prompting still comes from the self-registration request issued by the bundled GUI-session agent.** This corrects the origin doc's implicit "bundle → native auto-populate" framing: R2's "no manual `+` add" is satisfied because the entry is *present to toggle*, driven by the agent's request — not by passive OS population. The System Settings deep-link remains a permanent path (Screen Recording prompt reliability is historically flaky across OS versions).
- **Freeze `com.screencap.daemon` as the permanent identity.** Every future identifier change re-orphans grants; pick it now and verify the synthesized designated requirement (`codesign -d -r-`) names it before shipping.
- **Preserve the daemon-version stamp + binary-resolution contract.** `screencap-cli-version` and `resolveBinary()` must track the new exec location so SCR-121 version reconciliation and CLI/daemon launch keep working.
- **The CPython entitlements (`allow-jit`, `allow-unsigned-executable-memory`, `disable-library-validation`) now ride on the privileged TCC subject.** They are retained for this work (dropping them is the deferred hardening task), but the binary that carries them is now the holder of the Screen Recording / Accessibility / Input Monitoring grants — a higher risk class than the bare CLI. The deferred hardening ticket treats this as its #1 item, and the accepted residual is recorded in `SECURITY.md` (U4).

---

## Open Questions

### Resolved During Planning

- Helper shape: agent-launched nested `.app` with `CFBundleIdentifier=com.screencap.daemon` (vs. `loginItem` / root daemon / `CREATE_INFOPLIST_SECTION_IN_BINARY` on the bare binary). Resolved via research — see Key Decisions + Alternatives.
- Does bundle id alone auto-prompt? No — prompting requires the bundled GUI-session agent to issue the request. Resolved; shapes U6.
- Grant migration on identifier change? One-time re-grant; same team does not save it. Resolved; shapes U7.

### Deferred to Implementation

- Exact `BUNDLE()` parameters and how `multiprocessing.spawn` resolves `sys.executable` when the frozen exec lives at `…/ScreencapDaemon.app/Contents/MacOS/screencap` — must be validated that spawned capture workers still launch and read live TCC (see Risks). `[Needs research]`
- Whether IM populates natively once the agent issues the event-tap touch from the bundled identity, or still needs the manual deep-link — confirm on-device (R8). `[Needs research]`
- Whether `AssociatedBundleIdentifiers` / environment constraints are needed for SMAppService to accept the nested-`.app` exec — verify via `sfltool dumpbtm` after registration. **Security-relevant:** this key enforces the parent-bundle (responsible-ancestor) constraint at launch; if accepted, set it to `com.screencap.macos`, else record the residual in `SECURITY.md`. `[Technical]`
- Does the running daemon (and a `multiprocessing.spawn` capture worker) present code identity `com.screencap.daemon` to TCC after launch — the single empirical fact R1/R2/R4 rest on. Verify via `codesign` of the live PIDs during U1's gate and U8. `[Needs research]`
- Does PyInstaller's `BUNDLE()` `Info.plist` set `CFBundleExecutable` to the actual Mach-O filename — if not, codesign can't locate the designated executable and the DR won't name `com.screencap.daemon`. Verify in U1 before U4 signing. `[Technical]`
- Whether the helper `.app` needs its own minimal entitlements distinct from `screencap-cli.entitlements`. `[Technical]`

---

## High-Level Technical Design

> *This illustrates the intended approach and is directional guidance for review, not implementation specification. The implementing agent should treat it as context, not code to reproduce.*

Target bundle layout (inside the outer `Screencap.app`):

    Screencap.app/Contents/
      Library/LaunchAgents/com.screencap.daemon.plist     # Release: BundleProgram → nested .app exec directly
      Library/LoginItems/ (or Helpers/) ScreencapDaemon.app/
        Contents/
          Info.plist        # CFBundleIdentifier = com.screencap.daemon
          MacOS/screencap    # PyInstaller bootloader Mach-O (TCC subject)
          Frameworks/        # _internal .dylib/.so payload (cross-linked)
          Resources/         # non-code data
          _CodeSignature/
      Resources/
        screencap-daemon-launcher   # Debug dev-source only (Release execs the nested exec directly)
        screencap-cli-version        # version stamp (path preserved or relocated + consumer updated)

Signing order (authoritative pass, inside-out): nested `Frameworks/*.dylib|*.so` → `ScreencapDaemon.app/Contents/MacOS/screencap` (w/ CPython entitlements) → `ScreencapDaemon.app` (w/ Info.plist) → outer `Screencap.app` → `notarytool` + `stapler`.

---

## Implementation Units

### U1. Emit a helper `.app` from the PyInstaller spec

**Goal:** Produce a `ScreencapDaemon.app` (bundle id `com.screencap.daemon`, exec `Contents/MacOS/screencap`, `_internal` cross-linked into `Frameworks/`) from the existing build, preserving `--onedir`/spawn behavior.

**Requirements:** R1

**Dependencies:** None

**Files:**
- Modify: `pyinstaller/screencap.spec`

**Approach:**
- Add a `BUNDLE()` step wrapping the existing `COLLECT` with `bundle_identifier='com.screencap.daemon'` and an `Info.plist` carrying `CFBundleIdentifier` + `CFBundleExecutable` (the latter must equal the actual Mach-O filename `screencap`). Keep `console=True`/`--onedir` semantics.
- Let PyInstaller own `Frameworks/`↔`Resources/` cross-linking; do not hand-place dylibs.
- The bare `dist/screencap/` COLLECT output may still be produced for non-app consumers (CLI tarball); the `.app` is the macOS-app artifact.

**Execution note:** This unit ends in a **go/no-go gate** (see Verification) — the spawn-inside-`.app` bet is the highest-uncertainty item, so it must pass here before U2–U7 are built. If it fails, pivot to the bare-binary or `loginItem` fallback (Alternatives) rather than discovering breakage at U8.

**Patterns to follow:** existing `EXE`/`COLLECT` structure and the excludes/hiddenimports already in `pyinstaller/screencap.spec`.

**Test scenarios:**
- Happy path: produced `.app` has `Contents/MacOS/screencap`, an `Info.plist` whose `CFBundleIdentifier=com.screencap.daemon` and `CFBundleExecutable=screencap`, and `_internal` content reachable under `Contents/Frameworks`/`Resources`.
- Edge case: `_provisioned` / optional packages absent on a dev build still produce a structurally valid bundle.
- Integration (go/no-go): a real `multiprocessing.spawn`-based recording started from the nested exec launches capture workers that produce real (non-wallpaper) frames, and `codesign` of the live worker PID resolves to `com.screencap.daemon`.
- Covers AE1 (precondition: a properly-bundled helper exists).
- Test expectation: shape assertions are folded into U4's `sign_app.sh` build-time checks and U8's runbook (no standalone static spec-shape test — a spec assertion can't see the real build artifact).

**Verification (go/no-go gate):** A local build yields `ScreencapDaemon.app`; `codesign -dvv` (post-U4) shows bundle id `com.screencap.daemon` with `Info.plist` bound; `… --version` runs from the nested exec; **and a spawned capture worker launches and reads live TCC under the helper identity**. Spawn failure or identity divergence here is a no-go → take the fallback before proceeding.

---

### U2. Embed the helper `.app` into the outer app and re-point the launcher

**Goal:** Update `embed-cli.sh` to place `ScreencapDaemon.app` in the outer bundle, write the launcher to exec the nested `.app`'s binary, and preserve the version stamp.

**Requirements:** R1, R7

**Dependencies:** U1

**Files:**
- Modify: `macos/Screencap/Scripts/embed-cli.sh`
- Modify: `macos/project.yml` (pre/post-build script paths if the embed destination or plist copy changes)

**Approach:**
- Replace the `ditto dist/screencap → Contents/Resources/screencap` step with embedding the `.app` at the chosen location (`Contents/Library/LoginItems/` or `Contents/Helpers/` — finalize against SMAppService acceptance in U3).
- **Release path: point `BundleProgram` directly at the nested `ScreencapDaemon.app/Contents/MacOS/screencap`, dropping the `/bin/sh` launcher indirection** so the launched + TCC-attributed process is the helper bundle itself (see Key Technical Decisions — the loose launcher in the outer app's `Resources` risks breaking responsible-code attribution). Keep the launcher script only for the **Debug dev-source** branch (which needs the `PYTHONPATH`/dev-python switch); the Debug TCC-attribution caveat is acceptable since dev builds re-key TCC anyway.
- Preserve `screencap-cli-version` stamping (keep its path, or relocate and update the consumer in U5).

**Patterns to follow:** existing `write_daemon_launcher()` Debug/Release split and `stamp_bundled_cli_version()`.

**Test scenarios:**
- Happy path: after embed, the nested `.app` + launcher + version stamp exist at expected paths.
- Edge case: Debug dev-source path still resolves in-repo `screencap` (dev workflow unbroken).
- Test expectation: covered by `tests/daemon/test_launchagent.py` updates (U3) + a shell-assert extension to the existing embed-script test.

**Verification:** Built app contains the nested `.app`; launcher execs it; `screencap-cli-version` present and correct.

---

### U3. Update the launch-agent plist + renderer to target the nested exec

**Goal:** Point `BundleProgram` at the executable inside the helper `.app` and keep the renderer/bundled-plist parity test green.

**Requirements:** R7

**Dependencies:** U2

**Files:**
- Modify: `macos/Screencap/Resources/com.screencap.daemon.plist`
- Modify: `src/screencap/daemon/launchagent.py`
- Test: `tests/daemon/test_launchagent.py`

**Approach:**
- Set `BundleProgram` to the nested `.app`'s `Contents/MacOS/screencap` (Release) per U2; keep `Label`, `RunAtLoad`, `ExitTimeOut`, `PATH`-only env, and the no-`~`-keys rule.
- `AssociatedBundleIdentifiers` is left as an on-device verify (Open Questions), not added speculatively — but it carries a security dimension (it enforces the responsible-ancestor / parent-bundle constraint at launch). If the U8 verify shows SMAppService accepts/needs it, set it to `com.screencap.macos`; otherwise record the accepted residual in `SECURITY.md`.
- Update `render_plist(...)` so the bundled plist still matches byte-for-byte.

**Patterns to follow:** existing `render_plist` `bundle_program` handling.

**Test scenarios:**
- Happy path: `render_plist(bundle_program=<new path>)` equals the bundled plist (parity test).
- Edge case: required stable keys present; no launchd-disallowed extras (existing test retained).
- Covers F1 (the agent launches the bundled helper).

**Verification:** Parity test passes; on a built app, `SMAppService.agent` registers and `launchctl print gui/<uid>/com.screencap.daemon` shows it running from the nested exec.

---

### U4. Extend inside-out signing + notarization to the helper `.app`

**Goal:** Sign the nested `.app` (dylibs → inner exec w/ CPython entitlements → `.app` w/ Info.plist) before the outer app, and notarize the whole, with a stable DR naming `com.screencap.daemon`.

**Requirements:** R1, R3

**Dependencies:** U2

**Files:**
- Modify: `script/sign_app.sh`
- Modify: `macos/Screencap/Scripts/embed-cli.sh` (`sign_embedded_cli` for the Xcode-build signing path)
- Modify (if needed): `script/notarize_app.sh`
- Modify: `SECURITY.md` (record the accepted residual — CPython entitlements on the new TCC subject)

**Approach:**
- Update both signing paths (the Xcode preBuild `sign_embedded_cli` and the authoritative `sign_app.sh`) to walk the nested `.app`: sign `Frameworks/*.dylib|*.so`, then `Contents/MacOS/screencap` with `screencap-cli.entitlements`, then the `.app`, then the outer app. The "no `--deep`" rule applies to the **signing** passes only.
- **`sign_app.sh` pins the old path (`CLI_DIR="${APP_PATH}/Contents/Resources/screencap"`) in ~6 load-bearing sites** — the existence guard, the nested-dylib `find`, the entitlement sign, the team-identity check, the `--verify` step, and the post-sign `--version` smoke test. Update **every** site to the relocated nested-`.app` path; a partial update either aborts the build on the existence guard or silently signs nothing and ships an ad-hoc inner exec.
- Keep `--options runtime --timestamp`; **retain** `codesign --verify --deep --strict` (verify-deep is fine and is now the only check that recurses into the nested `.app`) — do not confuse the no-`--deep`-signing rule with the verify step.
- Add an explicit pre-outer-verify assertion that the nested exec's signing identity is the **Developer ID** (not ad-hoc) and that `codesign -d -r- ScreencapDaemon.app` names `identifier "com.screencap.daemon"` + the team id; replicate the `… --version` smoke test at the nested exec's new path. Gate the Release build on the nested `.app` existing at the expected path.
- Keep current entitlements (no `disable-library-validation` change here); record in `SECURITY.md` that these entitlements now ride on the TCC subject and are the #1 deferred-hardening item.

**Patterns to follow:** existing `sign()` helper, inside-out order, and `--verify --deep --strict` final check in `script/sign_app.sh`.

**Test scenarios:**
- Happy path: a signed build passes `codesign --verify --deep --strict` on the nested `.app` and the outer app; DR names `com.screencap.daemon`; the nested exec's identity == Developer ID.
- Error path: signing with `DEVELOPMENT_TEAM`/identity unset fails loudly (no silent ad-hoc fallback for Release); a missing nested `.app` aborts rather than producing a partial signature.
- Error path: the nested exec signed ad-hoc (incomplete path update) is caught by the pre-outer-verify identity assertion, not shipped.
- Covers AE2 (bundle id is real and resettable).
- Test expectation: build-time shell assertions in `sign_app.sh` (no standalone static sign-shape test — it can't see the signed artifact).

**Verification:** `notarytool` accepts the app; `spctl -a -vv` clean; the nested exec reports the Developer-ID identity; `tccutil reset ScreenCapture com.screencap.daemon` succeeds on an install.

---

### U5. Update binary resolution + version-stamp consumers

**Goal:** Resolve the daemon/CLI binary at its new in-`.app` location across Swift and any Python consumers, preserving dev fallbacks and SCR-121 version reconciliation.

**Requirements:** R7

**Dependencies:** U2

**Files:**
- Modify: `macos/Screencap/Controllers/CLIClient.swift` (`resolveBinary()`)
- Modify (if version stamp relocated): `macos/Screencap/Controllers/DaemonInstallController.swift` (`BundledDaemonVersion`)
- Test: `macos/ScreencapTests/CLIRecorderServiceTests.swift` / `DaemonInstallControllerTests.swift`

**Approach:**
- Update the bundled-path branch to the nested `.app` exec; keep `SCREENCAP_CLI_PATH` override and Debug dev fallback precedence.
- If `screencap-cli-version` moves, update `BundledDaemonVersion.resourceName`/lookup; otherwise leave as-is.

**Patterns to follow:** existing `resolveBinary()` precedence chain and `BundledDaemonVersion.expected()`.

**Test scenarios:**
- Happy path: resolver returns the nested exec when present.
- Edge case: `SCREENCAP_CLI_PATH` override still wins; Debug dev fallback unchanged.
- Error path: missing binary surfaces the existing "searched paths" diagnostic.

**Verification:** App launches the daemon via the resolved path; version gate reports a match (no false `daemonVersionMismatch`).

---

### U6. Confirm self-registration fires from the bundled agent identity + align onboarding copy

**Goal:** Ensure the retained `permission.request` path triggers prompts/listing from the new bundled GUI-session identity, and the walkthrough copy reflects "toggle the present entry" + deep-link fallback (no `+` add).

**Requirements:** R2, R4, R5

**Dependencies:** U3, U4

**Files:**
- Verify (likely no logic change): `src/screencap/daemon/permission_register.py`, `src/screencap/daemon/app.py`, `src/screencap/engine/platform/darwin.py`
- Modify (copy): `macos/Screencap/Views/Privacy/FirstRunPermissionsView.swift`, `macos/Screencap/Controllers/PermissionController.swift` (helper-entry-name strings now that the entry is `com.screencap.daemon`)
- Test: `macos/ScreencapTests/PermissionControllerTests.swift`

**Approach:**
- Keep the three registration mechanisms; they now run as the bundled agent.
- **Input Monitoring: commit to issuing the real `CGEventTapCreate` (listen-only) touch from the bundled identity** — the spike showed `IOHIDRequestAccess` alone does *not* register IM, and R4 forbids degrading IM to manual-only. Pre-state the branch: if U8 shows IM still won't populate even via the event-tap from the bundled identity, the decision is escalated (relax R4 to deep-link-assisted, or block the release) rather than left as an open on-device question.
- Update `helperSettingsEntryName`/rationale copy if the System Settings row label changes under the bundle id; keep the deep-link path as the standing fallback.

**Patterns to follow:** existing `requestDaemonPermission` → ack → open-pane flow.

**Test scenarios:**
- Happy path: `PermissionController` maps each pane to `com.screencap.daemon` and the Grant flow issues the daemon request then opens the pane.
- Edge case: daemon-registration failure still opens the pane (deep-link fallback) — existing behavior preserved.
- Covers AE3 (fallback still surfaces a toggleable entry).

**Verification:** On-device, clicking Grant lands the user on a pane with a present, toggleable `com.screencap.daemon` entry for all three permissions.

---

### U7. One-time re-grant migration + clean cutover

**Goal:** On upgrade from the bare-`screencap` identity, evict the prior-identity daemon + stale socket and drive a single re-grant, with tester comms.

**Requirements:** R6

**Dependencies:** U3, U5

**Files:**
- Modify: `macos/Screencap/Controllers/DaemonInstallController.swift` (cutover/bootout sequencing for the identity change)
- Create: `docs/solutions/build-errors/daemon-tcc-identity-migration-2026-06-30.md` (standalone migration note + tester comms, incl. the orphaned-row cleanup instruction)
- Test: `macos/ScreencapTests/DaemonInstallControllerTests.swift`

**Approach:**
- Reuse the existing `bootout` of `gui/<uid>/com.screencap.daemon` + socket cleanup + version reconciliation so the new helper binds cleanly instead of racing a surviving old daemon.
- **Ensure the old bare exec at `Contents/Resources/screencap/screencap` is removed/overwritten by the upgrade** so the headless auto-spawn path (`screencap status` → `posix_spawn`) cannot resurrect the old-identity daemon and race the new helper for the socket.
- **Enumerate the full cutover failure surface in the migration note:** (a) orphaned old-identity `screencap` csreq TCC rows survive the identifier change and can't be cleared by `tccutil reset … com.screencap.daemon` — include a one-time `tccutil reset All screencap` in tester comms (note it targets the *old* identifier); (b) re-approve the helper in Login Items if SMAppService flags it; (c) relaunch/kickstart for the per-process TCC cache.
- Surface the three permissions as not-granted post-upgrade (existing walkthrough/recovery banner).
- **Peer-plan reconciliation (review finding #4 — flagged for confirmation):** do *not* rewrite `docs/plans/2026-06-03-001-…`'s scope. U4 relocates the embedded-CLI path that plan's signing/notarization runbook + System-Wide Impact still describe as `Contents/Resources/screencap/screencap`; the migration note above records this so the stale path is reconciled when that plan is next executed. (Alternative if you prefer: a one-line factual path correction directly in that plan's interaction-graph section.)

**Patterns to follow:** existing `LaunchctlDaemonTerminator.bootout()` + `pollDaemon` convergence (SCR-135).

**Test scenarios:**
- Happy path: upgrade path boots out the stale daemon, registers the new helper, and presents permissions as not-granted.
- Edge case: stale daemon slow to exit (ExitTimeOut) converges within budget without a false `daemonVersionMismatch`.
- Covers AE4 (single re-grant restores recording).

**Verification:** On a machine with the old build's grants, updating drives one re-grant pass and recording works after.

---

### U8. On-device clean-identity validation runbook (R8)

**Goal:** A documented, reproducible procedure validating native registration for all three permissions against the shipped Developer-ID + hardened-runtime identity on a clean state — closing the spike's ad-hoc-only gap.

**Requirements:** R8

**Dependencies:** U6, U7

**Files:**
- Create: `docs/research/2026-06-30-daemon-helper-bundle-ondevice-validation.md` (runbook + results)

**Approach:**
- Mirror the spike's confound-free method (fresh/clean identity, agent under launchd, never shell-run): reset `com.screencap.daemon` TCC, clear `~/.screencap` aside, build+sign+notarize, then verify each pane shows a toggleable entry and post-grant recording works without `kickstart`.
- **Validate from a quarantined / Gatekeeper-translocated download** of the notarized app (not only a clean dev-installed build) — the origin required avoiding App Translocation, and a translocated app runs from a randomized read-only path that shifts the nested exec path, `sys.executable` for spawn, and `resolveBinary()`. This is the actual first-run new-user (A1) case.
- **Record the TCC subject identity of the running processes:** `codesign` the live daemon PID *and* a spawned capture-worker PID and confirm both resolve to `com.screencap.daemon` (not `/bin/sh`, the loose launcher, or a divergent cdhash) — this is the single empirical fact R1/R2/R4 rest on.
- Record per-permission results (esp. whether Input Monitoring populates natively via the event-tap touch vs. needs the deep-link — feeds the U6 escalation branch).

**Patterns to follow:** `docs/research/2026-06-05-daemon-tcc-registration-spike.md` structure.

**Test scenarios:** Test expectation: none — manual on-device QA deliverable (documented results, not an automated test). Recording-pipeline sanity can reuse the `capture-test` skill once grants are in place.

**Verification:** Runbook completed with PASS for SR + Accessibility native listing, an explicit IM result, a successful post-grant recording from a **translocated/quarantined** install, and confirmation that the live daemon PID and a spawned worker PID both `codesign` as `com.screencap.daemon`.

---

## System-Wide Impact

- **Interaction graph:** SMAppService agent registration → launchd execs the nested `.app`'s `Contents/MacOS/screencap` **directly** (Release; no `/bin/sh` launcher) → daemon serves over UDS; CLI/`CLIClient` and `DaemonInstallController` resolve the binary/version; capture workers spawned via `multiprocessing.spawn` re-exec the frozen binary and must inherit the `com.screencap.daemon` TCC identity.
- **Error propagation:** signing/notarization failures must fail the build loudly (no ad-hoc fallback for Release); an ad-hoc-signed nested exec is caught by U4's pre-outer-verify identity assertion; daemon-version mismatch keeps the existing typed `installFailed` states.
- **State lifecycle risks:** stale prior-identity daemon + `~/.screencap/run/api.sock` during cutover (U7); orphaned old-identity `screencap` TCC rows + the headless auto-spawn path potentially resurrecting the removed old exec (U7); per-process TCC cache requires relaunch after grant.
- **API surface parity:** `permission.request` verb + `daemon.info` grant block unchanged; only the subject identity changes.
- **Integration coverage:** spawn-inside-`.app` worker launch and live-TCC read are not provable by unit tests — covered by U8 on-device validation.
- **Unchanged invariants:** outer app bundle id `com.screencap.macos`; permission model + microphone ownership; `--onedir`/spawn architecture; daemon launched as an **agent** (GUI session), not a root daemon.

---

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| `multiprocessing.spawn` workers fail to re-exec when the frozen binary lives inside the nested `.app` (path/`sys.executable` resolution) | **U1 go/no-go gate** validates spawn before U2–U7; keep `--onedir`; explicit-exec-path fallback; if it fails, pivot to bare-binary/`loginItem`. Highest-uncertainty item. |
| Spawned worker's TCC subject identity diverges from the granted helper — "looks granted but records wallpaper" | `codesign`-verify the live daemon PID *and* a spawned worker PID resolve to `com.screencap.daemon` in U1's gate and U8 (not just that registration/launch succeeded). |
| Launcher-script indirection (`/bin/sh` in outer-app `Resources`) breaks responsible-code attribution | Release `BundleProgram` points directly at the nested `.app` exec; launcher retained only for Debug dev-source (U2/U3). |
| Bundle id alone does not make Screen Recording / Input Monitoring auto-prompt | Drive the request from the bundled GUI-session agent (U6); keep the System Settings deep-link as the permanent fallback; validate per-OS in U8. |
| App Translocation shifts the nested-exec/spawn/`resolveBinary()` paths on a quarantined first-run download | U8 validates from a Gatekeeper-translocated download, not only a clean dev install; notarize + staple so Gatekeeper doesn't translocate the shipped artifact. |
| Orphaned old-identity (`screencap`) TCC rows + headless auto-spawn resurrecting the old binary | U7 removes/overwrites the old exec; migration note includes `tccutil reset All screencap` for testers. |
| SMAppService rejects a `BundleProgram` pointing into a nested `.app` (constraints/placement) | Verify via `sfltool dumpbtm` (U3 deferred check); fall back to `loginItem` registration (see Alternatives) if the agent form fails. |
| PyInstaller dylib signing/placement breakage under hardened runtime (`no cdhash`, LV failures) | Let PyInstaller seed inner signatures; authoritative inside-out re-sign in U4; keep current CPython entitlements. |
| One-time re-grant churns existing testers | Accepted + communicated (U7); reuse stale-daemon eviction so re-grant is a single clean pass. |
| Notarization stalls release | `notarize_app.sh` already exists; U4 only extends coverage to the nested `.app`. |

---

## Alternative Approaches Considered

- **Re-sign the bare binary with `--identifier com.screencap.daemon` (+ `CREATE_INFOPLIST_SECTION_IN_BINARY`), no `.app`.** Lower effort; makes `tccutil` target work and TCC key on the bundle id. Rejected as the primary: an embedded info-plist section is not a true bundle, so it likely does **not** satisfy the "bundled code in GUI session" prompting requirement and gives no responsible-code ancestor — it would not reliably fix R2/R4. Retained as a possible fallback if the `.app`+spawn path proves intractable.
- **`SMAppService.loginItem` (`Contents/Library/LoginItems/ScreencapDaemon.app`) instead of an agent plist.** Cleanest TCC subject and GUI-session. Rejected as primary only to minimize churn to the working `DaemonInstallController` agent flow; promoted to fallback if the agent + nested-exec `BundleProgram` form is rejected by SMAppService (U3 risk). **Concrete pivot if taken:** embed the `.app` at `Contents/Library/LoginItems/` (U2); change `DaemonInstallController` registration from `SMAppService.agent(plistName:)` to `SMAppService.loginItem(identifier: "com.screencap.daemon")`; drop the `com.screencap.daemon.plist` + its renderer/parity test (U3) since loginItem registers by identifier with no authored plist; the bootout/version-reconciliation logic adapts to the loginItem registration state. This is a contained pivot localized to U2/U3/U5/U7 — pre-scoped here so a U3 rejection is not a re-plan.
- **`SMAppService.daemon` (root LaunchDaemon).** Rejected outright — runs outside the GUI session, so it can never trigger TCC prompts for Screen Recording / Input Monitoring.

---

## Phased Delivery

### Phase 1 — Packaging & identity
- U1 (PyInstaller `.app`) **→ go/no-go gate** (spawn-inside-`.app` launches + worker reads live TCC under the helper identity). Only on PASS proceed to U2 (embed + Release `BundleProgram` → nested exec), U3 (plist/renderer), U4 (signing/notarization), U5 (binary resolution). Lands the bundle identity, signing, and launch wiring. On gate FAIL, take the bare-binary/`loginItem` fallback before building U2–U7.

### Phase 2 — Registration, onboarding & migration
- U6 (self-registration from bundled identity + copy), U7 (one-time re-grant cutover), U8 (on-device validation runbook). Closes R2/R4/R6/R8.

---

## Sources & References

- **Origin document:** [docs/brainstorms/2026-06-30-daemon-helper-bundle-tcc-registration-requirements.md](docs/brainstorms/2026-06-30-daemon-helper-bundle-tcc-registration-requirements.md)
- Spike: `docs/research/2026-06-05-daemon-tcc-registration-spike.md`
- Distribution plan: `docs/plans/2026-06-03-001-feat-macos-app-developer-id-notarized-distribution-plan.md`
- Linear: SCR-196 — https://linear.app/zk-email/issue/SCR-196
- Apple DTS forums: [732291](https://developer.apple.com/forums/thread/732291), [128641](https://developer.apple.com/forums/thread/128641), [698337](https://developer.apple.com/forums/thread/698337), [702351](https://developer.apple.com/forums/thread/702351)
- [PyInstaller feature-notes](https://pyinstaller.org/en/stable/feature-notes.html)
