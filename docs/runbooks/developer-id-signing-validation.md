---
title: "Runbook: Developer ID signing validation — TCC grants survive rebuild (SCR-49 pre-flight gate)"
date: 2026-06-29
type: runbook
plan: docs/plans/2026-06-29-003-feat-scr-49-tcc-migration-entitlement-drop-plan.md
related_plan: docs/plans/2026-06-03-001-feat-macos-app-developer-id-notarized-distribution-plan.md
learning: docs/solutions/build-errors/macos-ad-hoc-signing-tcc-rebuild-treadmill.md
status: ready
unit: U1 / U5 (AE2 acceptance)
---

# Runbook: Developer ID signing validation (SCR-49 pre-flight gate)

**This is a gate, not a build guide.** It proves one thing before Phase 1c (SCR-49)
ships: that a Developer-ID-signed `screencap` daemon binary keeps its TCC grants
(Screen Recording / Accessibility / Input Monitoring) **across a rebuild + reinstall**,
with no re-prompt. That property — AE2 — is the strategic payoff of the daemon
refactor and the entire justification for dropping the SwiftUI app's role as a TCC
subject. If grants still clobber on rebuild, the migration banner Phase 1c ships
makes a promise the build can't keep, and the release must **not** proceed.

> **Forward-only release.** Once Phase 1c ships and existing users migrate, rolling
> back forces a *third* TCC re-prompt cycle on already-migrated users. This gate is
> the explicit guard: do not merge the SCR-49 code units until this runbook passes
> end-to-end. Fix forward on production issues.

The build/sign/notarize **mechanics** (the `script/sign_app.sh`, `script/notarize_app.sh`,
and CI workflow) are owned by the Developer ID distribution plan
(`docs/plans/2026-06-03-001-feat-macos-app-developer-id-notarized-distribution-plan.md`).
This runbook uses them where available and falls back to the explicit `codesign` /
`notarytool` commands below. Its unique contribution is the **TCC-persistence
verification loop** — the part the distribution plan does not cover.

## Why this gate exists (the one-paragraph version)

TCC anchors a grant to the requesting binary's **code-signing identity**. An ad-hoc
signature has no stable Team Identifier, so TCC falls back to the signature *digest*,
which changes on every rebuild → every rebuild looks like a brand-new app → prior
grants orphan. A stable **Developer ID Application** identity makes the daemon
binary's Designated Requirement identity-anchored (`identifier screencap and anchor
apple generic and certificate leaf[...] = "Developer ID Application: …"`), so a
rebuild with the same identity satisfies the same requirement and TCC keeps the grant.
The capture subject is **not** the `.app` — it is the nested
`Contents/Resources/screencap/screencap` binary the daemon runs. Full background:
`docs/solutions/build-errors/macos-ad-hoc-signing-tcc-rebuild-treadmill.md`.

## Prerequisites

- A **Developer ID Application** certificate installed in the build machine's keychain
  (`security find-identity -v -p codesigning` lists it). Note its Team ID.
- An App Store Connect API key (`.p8` + key-id + issuer-id) for `notarytool`
  (per the distribution plan's Key Technical Decisions).
- A **clean macOS 13+ machine** (or a VM / a fresh user account) for Step 3 — "clean"
  means ScreenCap has never been installed and has no leftover TCC rows. A reused
  machine carries stale grants that mask the very treadmill this gate checks for.
- `DEVELOPMENT_TEAM` set to the Developer ID Team ID. **Watch the `.env` foot-gun:**
  an `export ` prefix on the `.env` line silently drops the variable and lands you
  back on ad-hoc signing — see
  `docs/solutions/build-errors/env-export-prefix-silently-disables-team-signing.md`.

---

## Step 0 — Pre-flight: confirm the build is actually Developer-ID-signed

Before any TCC test, confirm both the app **and the nested binary** carry the team
identity. Signing the app but not the nested binary is the most common silent failure
(Xcode does not recurse into `Contents/Resources/`; `embed-cli.sh` re-signs it, but
only when `EXPANDED_CODE_SIGN_IDENTITY` resolves to a real identity).

```bash
APP=/path/to/ScreenCap.app
CLI="$APP/Contents/Resources/screencap/screencap"

# Both must show "TeamIdentifier=<your team>" and NOT "Signature=adhoc".
codesign -dvv "$APP"  2>&1 | grep -E "Identifier|TeamIdentifier|Signature"
codesign -dvv "$CLI"  2>&1 | grep -E "Identifier|TeamIdentifier|Signature"

# The daemon binary's Designated Requirement must be identity-anchored, not cdhash:
codesign -d --requirements - "$CLI" 2>&1
#   expect: designated => identifier screencap and anchor apple generic
#           and certificate leaf[subject.CN] = "Developer ID Application: …"
```

**Abort condition:** if `$CLI` shows `Signature=adhoc` / `TeamIdentifier=not set`, or
the DR is cdhash-anchored, the build is wrong. Fix signing (distribution plan U1 /
`embed-cli.sh`) before continuing. Do not proceed to the TCC test — it would "pass"
for the wrong reason on a reused machine and "fail" confusingly on a clean one.

---

## Step 1 — Build + notarize the first artifact

Build a **Release** configuration (Release makes `embed-cli.sh` emit the
bundled-binary-only daemon launcher, the shape shipped to users):

```bash
# Mechanics owned by the distribution plan. When its scripts exist:
#   script/sign_app.sh "$APP"        # inside-out Developer ID signing, hardened runtime
#   script/notarize_app.sh "$APP"    # notarytool submit --wait → stapler staple
#
# Until then, the explicit path:
#   1. Build Release (PyInstaller CLI → xcodegen → xcodebuild -configuration Release)
#   2. ditto -c -k --keepParent ScreenCap.app ScreenCap.zip
#   3. xcrun notarytool submit ScreenCap.zip --key … --key-id … --issuer … --wait
#   4. xcrun stapler staple ScreenCap.app
```

Confirm notarization actually succeeded (a stapled app is the only thing that
installs without Gatekeeper friction on the clean machine):

```bash
xcrun stapler validate "$APP"
spctl --assess --type execute --verbose "$APP"   # expect: accepted, source=Notarized Developer ID
```

**Abort condition:** notarization `Invalid` → fetch and read `xcrun notarytool log`;
do not ship un-notarized. Resolve before continuing.

---

## Step 2 — Install on the clean machine and grant Screen Recording

1. Copy the stapled `ScreenCap.app` to the clean macOS 13+ machine and launch it.
2. Run the first-run flow: approve the **ScreenCap helper** (daemon) install, then
   **Grant** Screen Recording for the helper.
3. Read **ground truth from the daemon**, not System Settings (System Settings can
   show a granted row for the wrong subject — see the learning's "three quirks"):

```bash
curl -s --unix-socket ~/.screencap/run/api.sock http://localhost/v0/daemon.info \
  | python3 -m json.tool
# expect: "permissions":{"screen_recording":"granted", …}
```

Gotchas that make a *correct* grant look broken (do not mistake these for a treadmill
failure — they are UI quirks documented in the learning):
- **Per-pane name divergence:** Screen Recording shows as **"ScreenCap"**;
  Accessibility / Input Monitoring show as lowercase **`screencap`**.
- **Default-OFF + stale pane:** a freshly-registered Screen Recording row is OFF and
  the pane does not live-refresh — `Cmd+Q` System Settings and reopen to toggle it.
- **Input Monitoring** does not self-register under launchd; add it with the **`+`**
  button. It is advisory and never blocks recording — don't chase it first.

**Do not rebuild between attempts in this step.** Grant once, confirm `granted`, then
proceed to Step 3. (Rebuilding mid-grant muddies which signature owns the grant.)

---

## Step 3 — Rebuild with the SAME identity, reinstall, verify persistence

This is the actual gate.

1. Rebuild the `screencap` binary + app with the **same Developer ID identity**
   (same Team ID, same cert). Re-notarize + staple as in Step 1.
2. Confirm the nested binary still carries the team identity (Step 0 commands).
   The cdhash will differ from the first build; the **Team ID and DR must not**.
3. Reinstall over the clean machine's existing install (replace `ScreenCap.app`,
   restart the helper):

```bash
launchctl kickstart -k gui/$(id -u)/com.screencap.daemon
```

4. Re-read daemon ground truth **without re-granting anything**:

```bash
curl -s --unix-socket ~/.screencap/run/api.sock http://localhost/v0/daemon.info \
  | python3 -m json.tool
# PASS  → "screen_recording":"granted"  (grant survived the rebuild — AE2 holds)
# FAIL  → "screen_recording":"denied"   (treadmill — grant orphaned on rebuild)
```

**PASS:** Screen Recording is still `granted` with no user re-prompt → the signing
pipeline anchors grants stably. **The SCR-49 pre-flight gate is satisfied; U2–U5 may
ship.**

**FAIL:** the grant flipped to `denied` → the signing identity is not stable across
rebuilds (cert mismatch, ad-hoc fallback on the nested binary, or a Team ID change).
**Do not ship Phase 1c.** File a signing-pipeline sub-issue (Linear, Screencap team)
and resolve before continuing — the migration banner cannot promise persistence the
build does not deliver.

---

## Step 4 — AE2 acceptance: five consecutive rebuilds (U5 sign-off)

Step 3 proves one rebuild cycle. AE2 / the SCR-49 acceptance bar is **≥5 consecutive
rebuilds** with no re-prompt. Repeat Step 3's rebuild → reinstall → verify loop five
times in a row, recording the result each cycle:

| Cycle | Nested-binary Team ID matches | `daemon.info` screen_recording | Re-prompted? |
|-------|-------------------------------|-------------------------------|--------------|
| 1     |                               |                               |              |
| 2     |                               |                               |              |
| 3     |                               |                               |              |
| 4     |                               |                               |              |
| 5     |                               |                               |              |

All five `granted` with no re-prompt → **AE2 passes; U5 acceptance is signed off.**
Any cycle re-prompting → treat as a signing-pipeline regression, halt the release,
file the sub-issue.

---

## Notes on cert rotation and the individual→org switch

The daemon binary's DR pins the **leaf certificate**, so:
- A Developer ID cert **rotation** (≈ yearly) re-grants **once** — expected, document
  it in release notes when it happens.
- The planned **individual → org Team ID** switch (see
  `docs/brainstorms/2026-06-03-individual-apple-dev-membership-tester-distribution-requirements.md`)
  orphans the daemon's grants and forces a one-time re-grant for every already-migrated
  tester. That is a deliberate, pre-announced "re-grant release" — the SCR-49 migration
  marker machinery (`~/.screencap/.tcc-migrated-v1`) is the natural vehicle to re-show
  the banner (bump to `.tcc-migrated-v2`), tracked as deferred follow-up, not part of
  this release.

## Related

- `docs/plans/2026-06-29-003-feat-scr-49-tcc-migration-entitlement-drop-plan.md` — the Phase 1c plan this gate guards (U1, U5).
- `docs/plans/2026-06-03-001-feat-macos-app-developer-id-notarized-distribution-plan.md` — owns the build/sign/notarize scripts + CI workflow.
- `docs/solutions/build-errors/macos-ad-hoc-signing-tcc-rebuild-treadmill.md` — the root-cause learning (TCC ↔ signing identity, the three UI quirks, per-subject reset).
- `docs/solutions/build-errors/env-export-prefix-silently-disables-team-signing.md` — the `.env` `export ` foot-gun that silently reverts to ad-hoc.
