---
title: "feat: Developer ID signing + notarized DMG distribution for the macOS app (individual account)"
type: feat
status: active
date: 2026-06-03
origin: docs/brainstorms/2026-06-03-individual-apple-dev-membership-tester-distribution-requirements.md
---

# feat: Developer ID signing + notarized DMG distribution for the macOS app (individual account)

## Summary

Build a manually-triggered CI workflow that signs the `ScreenCap.app` inside-out under an individual Developer ID (hardened runtime), notarizes it with `notarytool`, and ships a stapled DMG to testers — assembled from reusable local scripts, with the GitHub secret names as the single swap-surface so the later individual→org Team ID switch is a secrets change plus one planned permission re-grant.

---

## Problem Frame

The `.app` has no distribution path today: it is dev-only, signed per-developer via the `DEVELOPMENT_TEAM` env var, and falls back to ad-hoc signing that orphans TCC grants on every rebuild (`docs/solutions/build-errors/macos-ad-hoc-signing-tcc-rebuild-treadmill.md`). The product decision and guardrails — direct/notarized distribution under the individual account now, structured for a cheap org migration — are settled in the origin doc (see origin: `docs/brainstorms/2026-06-03-individual-apple-dev-membership-tester-distribution-requirements.md`). This plan resolves the two items the origin explicitly deferred to planning: the exact signing/notarization mechanism and credential storage, and whether the permissions walkthrough needs a copy tweak for the post-flip re-grant.

---

## Requirements

- R1. Tester builds are signed for direct distribution and notarized so they launch without Gatekeeper friction. *(origin R1, AE1)*
- R2. The bundle id stays `com.screencap.macos` across the individual→org switch. *(origin R2)*
- R3. Distribution stays direct (downloadable notarized DMG); no Mac App Store / App Store Connect record under the individual account. *(origin R3)*
- R4. Signing identity (Team ID), Developer ID certificate, and notary credentials are supplied via CI secrets / environment, never hardcoded — switching accounts changes no application source. *(origin R4, AE2)*
- R5. No team-scoped Apple capabilities (CloudKit, push, app groups, Sign in with Apple) are adopted while on the individual Team ID. *(origin R5)*
- R6. The org switchover ships as one deliberate "re-grant" release: testers are warned in advance and re-grant the three TCC permissions once via the existing walkthrough. The grants are **daemon-helper-owned** (`com.screencap.daemon`), not app-owned — re-signing the embedded daemon with the org Team ID orphans them and may also require re-approving the ScreenCap helper in Login Items (SMAppService). *(origin R6, F1, AE3)*

**Origin actors:** A1 (sole developer), A2 (tester cohort), A3 (individual Apple account), A4 (org Apple account)
**Origin flows:** F1 (org Team-ID flip, one-time)
**Origin acceptance examples:** AE1 (covers R1), AE2 (covers R4, R6), AE3 (covers R6)

---

## Scope Boundaries

- Mac App Store distribution — excluded now and near-term under the individual account.
- Auto-update (e.g. Sparkle) — out of scope; the org flip is a manual re-download for this cohort.
- No team-scoped Apple capabilities adopted (see R5).

### Deferred to Follow-Up Work

- **Universal / Intel (x86_64) tester DMG**: first build is arm64-only; a universal build needs both CLI arches embedded and signed — separate iteration.
- **Notarizing the standalone CLI tarball**: `scripts/install.sh` still prints the "not yet notarized … `xattr -cr`" hint for the CLI binary path; signing that distribution channel is separate from the `.app` and out of scope here.
- **Wiring app release into the tag-driven CLI release (`.github/workflows/release.yml`)**: deferred in favor of a decoupled workflow (see Key Technical Decisions); revisit if app and CLI cadences converge.

---

## Context & Research

### Relevant Code and Patterns

- `.github/workflows/release.yml` — tag-driven (`v*`) CLI release: macOS build matrix (`macos-14`/arm64, `macos-15-intel`/x86_64), PyInstaller build (`pyinstaller/screencap.spec`), `_smoke-test` gate, **minos verification step**, GCS auth via `secrets.GCP_SA_KEY`, upload to `gs://screencap-releases/releases/v<version>/`, GitHub Release with CHANGELOG extraction. Mirror its secret-wiring and GCS-upload conventions.
- `macos/project.yml` — xcodegen source of truth. `ENABLE_HARDENED_RUNTIME: YES` already set; `DEVELOPMENT_TEAM: ${DEVELOPMENT_TEAM}`; `CODE_SIGN_STYLE: Automatic`; bundle id `com.screencap.macos`; `CFBundleShortVersionString: "0.1.0"` / `CFBundleVersion: "1"` (hardcoded, not synced to CLI version or tag).
- `macos/ScreenCap/ScreenCap.entitlements` — minimal: only `com.apple.security.device.audio-input`. No sandbox, no team-scoped capabilities.
- `macos/ScreenCap/Scripts/embed-cli.sh` — preBuild phase: `ditto`s `dist/screencap/` (PyInstaller `--onedir`, many `.dylib`/`.so` + the `screencap` Mach-O) into `Contents/Resources/screencap/`, and writes a `screencap-daemon-launcher` shell script (Release variant execs only the bundled binary).
- `script/build_and_run.sh` — local build path: `xcodegen generate` → `xcodebuild build` into `.build/ScreenCapDerivedData/Build/Products/<config>/ScreenCap.app`. Plain `build` (no `archive`, no exportOptions.plist, no DMG). Reads `DEVELOPMENT_TEAM` from env / repo-root `.env`.
- `macos/ScreenCap/Controllers/CLIClient.swift` (per README) — `resolveBinary()` resolves `Contents/Resources/screencap/screencap` inside the bundle; the daemon launcher execs the bundled binary. Signing must preserve this path; App Translocation must be avoided.
- `.claude/skills/local-release`, `.claude/commands/release.md` — existing release orchestration (version bump + tag, or local build) to mirror, not duplicate.

### Institutional Learnings

- `docs/solutions/build-errors/macos-ad-hoc-signing-tcc-rebuild-treadmill.md` — **the why**: a stable Developer ID makes TCC track `(bundle id, Team ID)`; this pipeline is the prescribed long-term fix.
- `docs/solutions/build-errors/macos-pre14-binary-install-failure.md` — **`minos` is contagious** (bundle floor = max minos of any bundled binary). Any binary-touching sign step must run *after* minos verification. The arm64 CLI is minos 14.0 → distributed app effectively needs macOS 14+ (see Risks).
- `docs/solutions/build-errors/pyinstaller-frozen-binary-ci-failures.md` — re-run `screencap _smoke-test` against the **signed + stapled** artifact; hardened runtime changes which dylibs load. "Test what you bundle."
- `docs/solutions/runtime-errors/macos-tcc-per-process-cache-quit-and-relaunch.md` — TCC grants don't reach a running process; the post-flip re-grant requires the existing **Quit & Relaunch** path. Informs R6 UX.

### External References

- Sign **inside-out**, hardened runtime, `--options runtime --timestamp --entitlements`, **never `--deep`** (Apple TN2206, TN3127). Verify with `codesign --verify --strict` + `spctl --assess --type exec`.
- Notarize with `xcrun notarytool submit --wait` + App Store Connect API key (`.p8`/key-id/issuer-id); submit a `ditto -c -k --keepParent` archive (not `zip`); then `xcrun stapler staple` (Apple TN3147; notarytool man page). `altool` is retired.
- CI keychain: temp keychain + **`security set-key-partition-list`** (mandatory or `codesign` hangs); or `apple-actions/import-codesign-certs@v3`.
- **DMG** sign + notarize + staple avoids **App Translocation** and is cleanest for testers; macOS Sequoia 15.1+ removed the Ctrl-click bypass, so notarization is non-optional.
- Migration: bundle id stays; TCC keyed on `(bundle id, Team ID)` via Designated Requirement → org re-sign orphans grants once; a Team ID change alone does not trigger "app is damaged"; capability-minimal Developer ID apps need no provisioning profile and avoid CloudKit/app-group entanglement.

---

## Key Technical Decisions

- **Package as a signed + notarized + stapled DMG, not a raw zip**: the app resolves its bundled CLI by path; a DMG's drag-to-Applications gesture avoids App Translocation (which can break that resolution) and strips quarantine. Cleanest "open, no Gatekeeper" tester experience.
- **Separate, manually-triggered (`workflow_dispatch`) app-release workflow**, decoupled from the tag-driven CLI `release.yml`: app version (`0.1.0`) and tester cadence are independent of the CLI's stable semver (`0.20.0`); keeps tester builds off the stable-release critical path.
- **Reusable local sign + notarize/DMG scripts that CI invokes**: mirrors the `local-release` pattern; lets a build be cut when CI is unavailable and makes the signing path testable locally before wiring CI.
- **App Store Connect API key (`.p8`) for notarytool**, not an app-specific password: CI-friendly, no 2FA friction, revocable.
- **arm64-only for the first DMG**: testers are on Apple Silicon; universal/Intel deferred.
- **Hardened-runtime exceptions (`com.apple.security.cs.*`) are not team-scoped capabilities**: if the embedded PyInstaller CLI needs them, adding them does not affect the org migration. Actual capabilities (sandbox, CloudKit, push, app groups) stay at zero (R5).
- **GitHub secret names are the org-flip swap surface** (R4): the org switchover replaces the cert + Team ID + (if changed) notary secrets and re-runs the workflow — no source edits.
- **The permission-holding identity is the daemon helper (`com.screencap.daemon`), not the app (`com.screencap.macos`)**: `PermissionController` maps the three heavy grants to the helper, and `FirstRunPermissionsView` renders them as helper-owned. Signing, re-grant, and verification must target the daemon's identity; the org flip re-signs the embedded daemon (a separate signed helper registered via SMAppService, which may itself need re-approval in Login Items).
- **Library-validation is an open tension, not a settled exception** (see Open Questions): the existing `pyinstaller/entitlements.plist` marks `cs.disable-library-validation` *Required* for the spawn-mode CLI, but that exception weakens an app holding Screen Recording + Accessibility + Input Monitoring (a known DYLD-injection escalation primitive). Prefer full inside-out signing of every nested dylib so library validation passes without the exception; accept the exception only with documented justification.

---

## Open Questions

### Resolved During Planning

- Packaging format: **DMG** (signed + notarized + stapled).
- Release trigger: **`workflow_dispatch`** (manual), separate workflow.
- Notary auth: **App Store Connect API key (`.p8`)**.
- Certificate type: **Developer ID Application** (no provisioning profile needed — capability-minimal).
- Architecture: **arm64 first**.

### Deferred to Implementation

- Hardened-runtime entitlement exceptions for the embedded CLI: the known starting set is the existing `pyinstaller/entitlements.plist` (`cs.allow-jit` + `cs.disable-library-validation`, both marked *Required* for the spawn-mode frozen binary) — reuse/adapt it rather than authoring a new file or "starting with none." The open decision is whether full inside-out signing of all nested dylibs lets us **drop** `cs.disable-library-validation` (preferred, given the app's TCC weight) or whether it must be kept with documented justification; prove the answer against the `notarytool log` *and* the real launchd/SMAppService daemon-launch path, not the direct smoke test alone.
- Exact app version-stamping scheme (workflow input vs `git describe`): pick during U3. Whatever the scheme, `CFBundleVersion` must be **monotonically increasing** so the org-signed build is treated as an update, not a side-by-side install, and the value must be stamped into the `project.yml` info-properties source (the on-disk `Info.plist` carries a duplicate literal that must not be left to ship).
- Whether the macOS-14 floor is enforced by raising the distributed app's `LSMinimumSystemVersion` or by gating testers — **default to raising `LSMinimumSystemVersion` to 14.0** so macOS-13 testers are blocked at install with a clear message rather than silently receiving a build whose daemon is SIGKILLed (see Risks); revisit only if a macOS-13 tester is essential.

---

## Implementation Units

### U1. Local Developer ID signing script (inside-out, hardened runtime)

**Goal:** A script that signs a built `ScreenCap.app` — every embedded Mach-O/dylib in `Contents/Resources/screencap/` first, then the outer bundle — with a Developer ID identity supplied via env, producing a `codesign --verify --strict`- and `spctl`-clean app.

**Requirements:** R1, R2, R4, R5

**Dependencies:** None

**Files:**
- Create: `script/sign_app.sh`
- Reference / reuse: `pyinstaller/entitlements.plist` (existing — the embedded CLI's known starting entitlement set: `cs.allow-jit` + `cs.disable-library-validation`); only fork a macOS-app-specific copy if the org-app build genuinely diverges
- Reference: `macos/ScreenCap/ScreenCap.entitlements` (outer app, stays minimal — audio-input only)

**Approach:**
- Take the `.app` path and a signing identity (Developer ID Application) from env; never hardcode the Team ID.
- Enumerate and sign inner Mach-O/dylibs inside-out (`--options runtime --timestamp`), applying the embedded-CLI entitlements (`pyinstaller/entitlements.plist`) to the bundled `screencap` binary (which is also the daemon helper's exec target), then sign the outer `.app` with `--entitlements ScreenCap.entitlements`. Never `--deep`. Attempt to sign *all* nested dylibs with the same Developer ID so library validation can be satisfied without `cs.disable-library-validation` (see Key Technical Decisions).
- Leave the `screencap-daemon-launcher` shell script unsigned (scripts aren't Mach-O); confirm it still execs the bundled binary post-sign.
- Verify: `codesign --verify --deep --strict` and `spctl --assess --type exec`.

**Patterns to follow:** env-driven identity like `script/build_and_run.sh`'s `DEVELOPMENT_TEAM` handling; `ditto`/bundle-layout assumptions from `macos/ScreenCap/Scripts/embed-cli.sh`.

**Test scenarios:**
- Happy path: after signing a built app, `codesign --verify --deep --strict` exits 0 and `spctl --assess --type exec` reports `accepted`.
- Happy path: `codesign -dvv Contents/Resources/screencap/screencap` shows the expected Team ID (not `adhoc`).
- Edge case: signing an app whose `Contents/Resources/screencap/` is absent (dev build with no embedded CLI) fails loudly rather than silently producing a partial signature.
- Integration: Covers AE1. The signed embedded CLI runs under hardened runtime — `Contents/Resources/screencap/screencap _smoke-test` passes against the signed bundle. (Necessary but **not sufficient** — this is a direct exec; the real launchd/SMAppService daemon-launch path is exercised in U3, where spawn-mode/library-validation failures actually surface.)

**Verification:** A locally built app, after running this script with a real Developer ID identity, passes `codesign`/`spctl` checks and the embedded CLI smoke-test.

---

### U2. Local notarize + staple + DMG packaging script

**Goal:** A script that notarizes the signed app via `notarytool`, staples it, builds a DMG, and signs + notarizes + staples the DMG — yielding a `ScreenCap-<version>.dmg` that opens with no Gatekeeper friction offline.

**Requirements:** R1, R3

**Dependencies:** U1

**Files:**
- Create: `script/notarize_app.sh`

**Approach:**
- `ditto -c -k --keepParent ScreenCap.app ScreenCap.zip`; `xcrun notarytool submit --wait` with API-key env vars; on failure, fetch and print `notarytool log` for diagnosis.
- `xcrun stapler staple ScreenCap.app`; build DMG (`hdiutil`, UDZO); `codesign --timestamp` the DMG; notarize + `stapler staple` the DMG.
- Emit a `ScreenCap-<version>.dmg.sha256` checksum alongside the DMG (mirrors the CLI release's per-arch checksum convention) so testers/the runbook can verify the download out-of-band.
- Credentials (`.p8` content, key id, issuer id) read from env only.

**Patterns to follow:** GCS/secret-driven conventions from `.github/workflows/release.yml`; keep the script CI-invocable and locally runnable like `local-release`.

**Test scenarios:**
- Happy path: `notarytool` returns `Accepted`; `xcrun stapler validate` passes on both the `.app` and the `.dmg`.
- Happy path: Covers AE1. After copying the app out of a freshly-mounted DMG, `spctl --assess` reports `accepted` with no network (staple works offline).
- Error path: a deliberately unsigned nested binary causes `notarytool` to return `Invalid`, and the script surfaces the `notarytool log` rather than exiting 0.

**Verification:** Running U1 then U2 locally produces a stapled DMG; mounting it and launching the app shows no Gatekeeper block.

---

### U3. CI workflow: build → sign → notarize → DMG → upload

**Goal:** A `workflow_dispatch` GitHub Actions workflow on a `macos-14` runner that builds the arm64 CLI, embeds it, builds the app, runs U1/U2, and uploads the DMG — stamping the app version from the trigger.

**Requirements:** R1, R3, R4

**Dependencies:** U1, U2

**Files:**
- Create: `.github/workflows/release-macos-app.yml`
- Modify: `macos/project.yml` (drive `CFBundleShortVersionString`/`CFBundleVersion` from the workflow input — stamp the `info.properties` block, since `GENERATE_INFOPLIST_FILE: NO` means xcodegen merges those into the on-disk `macos/ScreenCap/Info.plist`; do not leave the stale `0.1.0`/`1` literal able to ship). `CFBundleVersion` must be **monotonically increasing** so the org build is treated as an update, not a side-by-side install.

**Approach:**
- Steps, in order (minos gate before any binary-touching sign step): build PyInstaller CLI → **minos verify** → `xcodegen generate` + `xcodebuild -configuration Release` (Release is required so `embed-cli.sh` emits the bundled-binary-only daemon launcher, not the Debug dev-source variant) → import Developer ID cert into a temp keychain (`security create-keychain` + **`set-key-partition-list`**, or `apple-actions/import-codesign-certs`) → `script/sign_app.sh` → `script/notarize_app.sh` → **exercise the real daemon-launch path** (register the helper via SMAppService / launchctl-bootstrap the plist and probe `/v0/daemon.info`, not just direct `_smoke-test` exec) → upload DMG **+ `.sha256`** to `gs://screencap-releases/app/` (a path prefix isolated from the CLI installer) and optionally attach to a GitHub **pre-release**.
- **CLI build-step reuse mechanism (decide before implementing):** `release.yml` has no `workflow_call` trigger and the repo has no composite actions, so "reuse" is not free. Either (a) extract the build + minos-verify steps into a composite action under `.github/actions/` (or a `workflow_call` reusable workflow) that both `release.yml` and this workflow call, or (b) accept copy-paste and flag the duplicated CVE-pin / constraints / minos blocks as a known maintenance cost. Prefer (a).
- **Secret-exposure guard:** restrict `workflow_dispatch` to the default branch (`if: github.ref == 'refs/heads/main'`) and/or run the signing job in a protected GitHub **Environment** with required reviewers, so a feature-branch dispatch can't sign attacker-modified scripts under the real Developer ID. Keep `permissions:` least-privilege as `release.yml` does.
- **Keychain hygiene:** delete the temp keychain in an `if: always()` step so cert material never lingers (and document that this workflow runs only on GitHub-hosted ephemeral runners).
- Workflow inputs: `version` (stamps the app), optional pre-release flag.
- Reference (do not hardcode) the secret-name contract: `MACOS_CERT_P12_BASE64`, `MACOS_CERT_PASSWORD`, `MACOS_KEYCHAIN_PASSWORD`, `MACOS_SIGN_IDENTITY`, `APPLE_NOTARY_KEY_P8_BASE64`, `APPLE_NOTARY_KEY_ID`, `APPLE_NOTARY_ISSUER_ID`, plus `GCP_SA_KEY` (audit its IAM scope before adding the new upload path; prefer a key scoped `objectCreator` on the `app/` prefix only).

**Execution note:** Validate `sign_app.sh`/`notarize_app.sh` locally (U1/U2) before wiring CI — notarization round-trips are slow to debug in Actions.

**Patterns to follow:** `.github/workflows/release.yml` matrix/build/minos/GCS-upload steps and `secrets.*` wiring.

**Test scenarios:**
- Test expectation: none (CI config) — verification is a green manual run.
- Integration: a `workflow_dispatch` run produces a stapled `ScreenCap-<version>.dmg` + `.sha256` under `gs://screencap-releases/app/`; `stapler validate` and `spctl --assess` pass on the downloaded artifact.
- Integration: the registered daemon helper actually starts from the signed/stapled app (SMAppService register succeeds and `/v0/daemon.info` responds) — proving the launchd path, not just direct exec, survives hardened-runtime signing.
- Integration: Covers AE2. The run uses only secrets/inputs for identity — `git grep` confirms no Team ID or notary credential is committed to source.
- Edge case: the built app's `Info.plist` `CFBundleShortVersionString` equals the workflow `version` input and `CFBundleVersion` is greater than the prior build's (monotonic stamping works).

**Verification:** A manual workflow run yields a downloadable notarized DMG; signing/notary identity comes entirely from secrets.

---

### U4. Org-flip migration runbook, secret-name contract, and tester comms

**Goal:** A runbook that documents how to produce the credentials, the local + CI signing flow, and the exact one-time org-flip procedure — making the "secrets swap + one re-grant release" concrete and repeatable.

**Requirements:** R2, R4, R5, R6 *(F1, A1, A2, A4; A3 is the swapped-out individual account)*

**Dependencies:** U3

**Files:**
- Create: `docs/runbooks/macos-app-developer-id-signing.md`
- Modify: `macos/README.md` (replace the stale "release pipeline in Unit 1 / Unit 22 of the v1 plan" reference with the real signing/distribution path)

**Approach:**
- Document: creating a Developer ID Application cert + App Store Connect API key; exporting each into the secret-name contract from U3; running the local scripts; triggering the CI workflow.
- **Org-flip section (F1):** the precise swap (replace `MACOS_CERT_P12_BASE64` / `MACOS_CERT_PASSWORD` / `MACOS_SIGN_IDENTITY` with org values; notary key if the account changes), re-run the workflow, then post the tester heads-up. Document that the org Team ID **re-signs the embedded daemon helper**, so testers must: (1) re-approve the ScreenCap helper in Login Items (SMAppService may flag `requiresApproval` / signing-invalid), and (2) re-grant the three **daemon-owned** permissions (`com.screencap.daemon`: Screen Recording + Accessibility + Input Monitoring) via **Quit & Relaunch** (per the per-process-TCC-cache learning). Include a step to quit the app and stop/unregister the old daemon and remove the stale `~/.screencap/run/api.sock` before first launch of the org build, so the new helper registers cleanly instead of racing a surviving individual-signed daemon. Provide a copy-paste Slack template referencing the **helper**, not just the app.
- State the macOS-14 floor decision from Risks (default: raise `LSMinimumSystemVersion` to 14.0) and how testers are told.
- **Tester-facing org-flip notes:** the Slack template should tell testers that (a) stale individual-signed entries for ScreenCap / its helper may remain visible in System Settings → Privacy after the flip and are harmless (toggling them does nothing — grant against the *new* entries), and (b) the one-time cost is "re-grant 3 permissions + re-approve the helper in Login Items," not just three permissions.
- **License-clean note:** record that the individual membership fully covers signing both identities + SMAppService + notarization (nothing is org-gated), and that the helper must stay a plain LaunchAgent — not a System/Endpoint-Security Extension — to keep the migration a pure re-sign.

**Test scenarios:** Test expectation: none (documentation). Reviewer check: the runbook lists every secret U3 consumes and the swap procedure references no source edits (validates R4's "secrets swap, no source"); a real Developer ID build is used to confirm the System Settings → Privacy entry shows a sensible helper name and SMAppService re-approval behaves as expected after a Team-ID change.

**Verification:** A reader can produce credentials, cut a tester DMG, and execute the org flip from the runbook alone.

---

### U5. Permissions walkthrough check for the one-time re-grant (origin deferred Q2)

**Goal:** Confirm the existing permissions walkthrough handles the orphaned **daemon-owned** grant state cleanly after an org-Team-ID re-sign, and add a minimal re-grant affordance only if a gap is found.

**Requirements:** R6 *(AE3)*

**Dependencies:** None (can run parallel to U1–U3)

**Files:**
- Modify (only if a gap is found): `macos/ScreenCap/Views/Privacy/` walkthrough view(s) and/or `macos/ScreenCap/Controllers/PermissionController.swift`
- Else: capture the "works as-is" finding in the U4 runbook.

**Approach:**
- Simulate the post-flip state by resetting the **helper** identity that actually holds the grants — `tccutil reset All com.screencap.daemon` (and `com.screencap.macos` for the app-level checks) — on an installed build, then confirm the walkthrough presents all three permissions as not-granted and the Quit & Relaunch path restores them. Also confirm the helper re-registers (SMAppService) rather than silently failing.
- If the flow is already correct, make no code change and record it; if copy is confusing for an *update* (vs first-run), add one line clarifying a recent update may require re-granting and re-approving the helper.

**Test scenarios:**
- Covers AE3. Integration: after `tccutil reset All com.screencap.daemon`, launching the app shows the three helper-owned permissions as not-granted, and a single walkthrough + Quit & Relaunch pass restores all three.
- Edge case: if copy is added, it appears only in the update/re-grant context and does not alter the first-run flow. (If no code change, `Test expectation: none -- verification-only unit`.)

**Verification:** The post-reset walkthrough is confirmed acceptable (or minimally adjusted), and the finding is recorded in the runbook.

---

## System-Wide Impact

- **Interaction graph:** the daemon helper (`com.screencap.daemon`) — launched by **launchd** via the `screencap-daemon-launcher` script (the plist `BundleProgram`), which execs `Contents/Resources/screencap/screencap` — is the identity that holds the three heavy TCC grants and is validated by SMAppService. Signing must preserve this path *and* keep the launchd/SMAppService launch path working (a direct exec does not exercise it); the DMG must prevent App Translocation that would relocate the bundle.
- **Error propagation:** notarization failures must surface the `notarytool log` (not exit 0); the post-sign smoke test must fail the build if hardened runtime breaks dylib loading.
- **State lifecycle risks:** stapling must occur on the `.app` before DMG packaging, and again on the DMG; an unstapled artifact fails offline Gatekeeper.
- **API surface parity:** the standalone CLI tarball remains unsigned (Deferred to Follow-Up) — the `install.sh` "not yet notarized" hint still applies to that path and is intentionally unchanged.
- **Unchanged invariants:** bundle id `com.screencap.macos`, application source (no Team ID in source), and capability-minimal entitlements are explicitly preserved across the org flip.

---

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| Embedded arm64 CLI is minos 14.0 while app deployment target is 13.0 → app window opens on macOS 13 but the **daemon helper** is SIGKILLed, stranding the user at daemon-install with no clear cause | Default to raising distributed `LSMinimumSystemVersion` to **14.0** so macOS-13 testers are blocked at install with a clear message (decide in U4); keep the minos-verify gate before signing |
| `cs.disable-library-validation` is *Required* by the spawn-mode PyInstaller CLI (per `pyinstaller/entitlements.plist`) but weakens an app holding Screen Recording + Accessibility + Input Monitoring (DYLD-injection escalation) | Attempt full inside-out signing of all nested dylibs so library validation passes **without** the exception; if it must be kept, document the justification and gate it on explicit sign-off (U1, Key Technical Decisions) |
| Post-sign verification runs a direct `_smoke-test` exec, but testers hit the **launchd/SMAppService** daemon path — a needed `cs.*` exception or signing-invalid failure surfaces only on a tester's machine | U3 gate registers the helper via SMAppService / launchctl and probes `/v0/daemon.info` against the signed-stapled app, not just direct exec |
| Outer-before-inner signing or `--deep` produces "app is damaged" for testers | Sign strictly inside-out; never `--deep`; verify with `codesign --verify --strict` + `spctl` before notarizing |
| App Translocation breaks bundled-CLI path resolution if a tester runs from the mounted DMG / Downloads instead of dragging to /Applications | Distribute via DMG (drag-to-Applications removes quarantine, avoids translocation); runbook instructs testers to move to /Applications; optional launch-time guard if it bites |
| Org flip orphans the **daemon's** TCC grants and may force SMAppService Login-Items re-approval | Single deliberate re-grant release: reset/re-grant `com.screencap.daemon`, re-approve the helper, clean stale daemon + socket, advance Slack comms + Quit & Relaunch (U4, U5) |
| Org-signed build coexists with a surviving individual-signed daemon (stale LaunchAgent, running process, `~/.screencap/run/api.sock`) | Monotonic `CFBundleVersion` (treated as update); runbook step to quit app + unregister/stop old daemon + remove stale socket before first org launch (U3, U4) |
| `workflow_dispatch` from a non-default branch could sign attacker-modified scripts under the real Developer ID; secrets exposed | Restrict dispatch to the default branch / protected Environment with required reviewers; least-privilege `permissions:`; secrets never echoed (U3) |
| Notary/cert secrets mishandled in CI | Ephemeral temp keychain + `set-key-partition-list` + `if: always()` `delete-keychain`; GitHub-hosted runners only; API key with Developer role; checksum (`.sha256`) published alongside the DMG; `GCP_SA_KEY` scoped to the `app/` prefix |

---

## Documentation / Operational Notes

- `docs/runbooks/macos-app-developer-id-signing.md` is the operational source of truth (U4); `macos/README.md` updated to point at it.
- After this lands, consider `/ce-compound` to capture notarization-stapling mechanics and an app-version policy — the learnings search found no existing doc for either.

---

## Sources & References

- **Origin document:** `docs/brainstorms/2026-06-03-individual-apple-dev-membership-tester-distribution-requirements.md`
- Current release pipeline: `.github/workflows/release.yml`
- App build + embed: `script/build_and_run.sh`, `macos/ScreenCap/Scripts/embed-cli.sh`, `macos/project.yml`
- Learnings: `docs/solutions/build-errors/macos-ad-hoc-signing-tcc-rebuild-treadmill.md`, `docs/solutions/build-errors/macos-pre14-binary-install-failure.md`, `docs/solutions/build-errors/pyinstaller-frozen-binary-ci-failures.md`, `docs/solutions/runtime-errors/macos-tcc-per-process-cache-quit-and-relaunch.md`
- External: Apple TN3147 (notarytool), TN3127 (code signing requirements / TCC DR), TN2206 (code signing in depth); notarytool man page; Eclectic Light Co. (Gatekeeper in Sequoia)
