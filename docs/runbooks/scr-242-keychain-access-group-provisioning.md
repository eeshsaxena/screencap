---
title: "Runbook: SCR-242 — authorize the daemon's keychain-access-groups entitlement with a Developer ID provisioning profile"
date: 2026-07-09
type: runbook
issue: SCR-242
related_issue: SCR-241
related_runbook: docs/runbooks/developer-id-signing-validation.md
status: ready
---

# Runbook: keychain-access-group provisioning (SCR-242)

**This is a required release step, not a build guide.** SCR-241 gave the daemon/CLI
helper the `keychain-access-groups: 2A8S6MV8DZ.com.screencap.shared` entitlement so
same-team binaries share the cloud refresh token without a per-binary Keychain ACL
prompt. That entitlement is a macOS **restricted entitlement**: AMFI **SIGKILLs** the
binary at launch (exit 137 / "Killed: 9") unless an **embedded Developer ID
provisioning profile** authorizes it. Without this runbook's profile, the shipped
daemon crash-loops under launchd — no recording, no auth (SCR-242).

Confirmed feasible for **non-sandboxed, direct-distribution (Developer ID)** helpers:
Apple's own [*Signing a daemon with a restricted entitlement*](https://developer.apple.com/documentation/xcode/signing-a-daemon-with-a-restricted-entitlement)
uses `keychain-access-groups` as its worked example. See also
[TN3125: Inside Code Signing — Provisioning Profiles](https://developer.apple.com/documentation/technotes/tn3125-inside-code-signing-provisioning-profiles).

> **Alternative if a profile is ever undesirable:** drop `keychain-access-groups`
> from `screencap-cli.entitlements` (and its two build-time assertions). The daemon
> then launches and auth falls back to the legacy `keyring` path — but the
> "ScreenCap wants to use screencap-auth" prompt SCR-241 killed (R4) returns on app
> updates. That is SCR-242 Option B; this runbook is Option A.

## What the code already does (this branch)

- `macos/ScreenCap/Scripts/screencap-cli.entitlements` declares, alongside
  `keychain-access-groups`, the two entitlements the profile must match:
  `com.apple.application-identifier = 2A8S6MV8DZ.com.screencap.daemon` and
  `com.apple.developer.team-identifier = 2A8S6MV8DZ`.
- `script/sign_app.sh` (the Developer ID release re-sign) **requires**
  `SCREENCAP_DAEMON_PROVISION_PROFILE`, copies it to
  `ScreencapDaemon.app/Contents/embedded.provisionprofile` **before** sealing the
  wrapper, seals the wrapper **with** `--entitlements` (so the entitlement survives
  the seal), then hard-asserts both the entitlement and the embedded profile and
  smoke-launches the entitled binary (an AMFI check — exit 137 fails the build).
- `macos/ScreenCap/Scripts/embed-cli.sh` (the Xcode build phase, which runs under
  Apple Development — where restricted entitlements are stripped) **defers** the
  whole keychain authorization to `sign_app.sh`; it no longer hard-asserts the group
  (that assertion failed every build — SCR-242 secondary bug #1). The release re-sign
  rebuilds every nested signature from scratch, so the build phase's entitlement
  state is not what ships.

So the only manual, one-time work is **creating the profile** and pointing the build
at it.

## Prerequisites

- Apple Developer **Account Holder / Admin** access to
  [developer.apple.com](https://developer.apple.com/account) for team `2A8S6MV8DZ`.
- A **Developer ID Application** certificate in the build keychain
  (`security find-identity -v -p codesigning`).
- The signing/notarization creds already sourced (`source ~/.zshrc`).

## Step 1 — Create the explicit App ID (portal)

Certificates, Identifiers & Profiles → **Identifiers** → **+**:

- Type: **App IDs** → **App**.
- Description: `ScreenCap Daemon`.
- Bundle ID: **Explicit** → `com.screencap.daemon` (must be explicit, **not** a
  wildcard — a wildcard App ID cannot authorize a restricted entitlement).
- **Leave Capabilities / App Services alone — there is NO "Keychain Sharing"
  capability to enable on a portal App ID** (that toggle exists only in *Xcode's*
  Signing & Capabilities UI). Keychain access groups are *implicit* to any explicit
  App ID: the Developer ID profile you create from it in Step 2 automatically
  authorizes `keychain-access-groups = 2A8S6MV8DZ.*` (the whole team namespace) +
  `com.apple.token`, and `2A8S6MV8DZ.*` covers our `2A8S6MV8DZ.com.screencap.shared`.
- Register.

## Step 2 — Create the Developer ID provisioning profile (portal)

Profiles → **+**:

- Distribution → **Developer ID** (this is the direct-distribution profile type; it
  is the one that works for a non-sandboxed, notarized helper).
- App ID: **`com.screencap.daemon`** (the explicit App ID from Step 1).
- Certificate: your **Developer ID Application** certificate.
- Name it e.g. `ScreenCap Daemon Developer ID` and **Generate** → **Download** the
  `.provisionprofile`.

Store it outside the repo (it is a signing input, not source). Verify it authorizes
the group:

```bash
security cms -D -i ~/path/to/ScreenCap_Daemon_Developer_ID.provisionprofile \
  | plutil -extract Entitlements xml1 -o - -
# expect entries for:
#   application-identifier            = 2A8S6MV8DZ.com.screencap.daemon
#   com.apple.developer.team-identifier = 2A8S6MV8DZ
#   keychain-access-groups            = ( 2A8S6MV8DZ.*, com.apple.token )
#     ^ the wildcard 2A8S6MV8DZ.* authorizes our 2A8S6MV8DZ.com.screencap.shared
#       (implicit to the explicit App ID — no capability to enable). A specific
#       group string here instead of the wildcard is equally fine.
```

## Step 3 — Point the build at it and sign

```bash
export SCREENCAP_DAEMON_PROVISION_PROFILE=~/path/to/ScreenCap_Daemon_Developer_ID.provisionprofile
export MACOS_SIGN_IDENTITY="Developer ID Application: Rute Figueiredo (2A8S6MV8DZ)"
script/sign_app.sh /path/to/ScreenCap.app
```

`sign_app.sh` fails loudly if the var is unset/missing, embeds the profile, and its
`--version` smoke launch is the first AMFI proof. The `macos-app-release` skill must
export `SCREENCAP_DAEMON_PROVISION_PROFILE` before it calls `sign_app.sh`.

Confirm the profile shipped in the bundle:

```bash
CLI_DIR=/path/to/ScreenCap.app/Contents/Library/LoginItems/ScreencapDaemon.app
test -f "$CLI_DIR/Contents/embedded.provisionprofile" && echo "profile embedded ✓"
codesign -d --entitlements :- "$CLI_DIR/Contents/MacOS/screencap" 2>/dev/null \
  | grep -q "2A8S6MV8DZ.com.screencap.shared" && echo "entitlement present ✓"
```

## Step 4 — Verify the entitled daemon actually launches (the real Q1)

This is the check SCR-241's U6 spike deferred, and the whole reason SCR-242 exists.
A direct-exec smoke passing (Step 3) is necessary but **not** sufficient — verify the
daemon launches under **launchd/SMAppService** on a clean machine (per
[developer-id-signing-validation.md](developer-id-signing-validation.md) Steps 1–2),
then:

```bash
launchctl kickstart -k gui/$(id -u)/com.screencap.daemon
sleep 2
# PASS: the daemon answers; FAIL: it crash-looped (AMFI kill) → socket is dead.
curl -s --unix-socket ~/.screencap/run/api.sock http://localhost/v0/daemon.info \
  | python3 -m json.tool
# Also confirm no "Killed: 9" / exit 137 in the daemon log:
tail -n 40 ~/.screencap/run/serve.log
```

Then verify the **keychain path** works end-to-end and prompt-free: `screencap login`
via the app, restart the daemon, confirm auth persists with **no** "ScreenCap wants to
use screencap-auth" prompt (the SCR-241 payoff). A frozen build that falls back logs a
WARN — `grep "fell back to legacy keyring" ~/.screencap/run/serve.log` should be empty.

**PASS** on all of the above → SCR-242 is resolved; the app release may proceed.
**FAIL** (exit 137 / dead socket) → the profile is wrong or unembedded; re-check
Steps 1–3 (explicit App ID, group registered, profile matches the cert, no `--deep`).

## Notes

- **No `--deep` signing** (already the case): it produces broken nested signatures
  and can strip the embedded profile.
- **Cert / Team rotation** re-issues the profile; regenerate it (Step 2) and it flows
  through unchanged. The daemon's DR still pins the leaf cert (see the TCC runbook).
- The **outer app** (`ScreenCap.entitlements`) deliberately does **not** declare
  `keychain-access-groups` (KTD-6), so it needs no profile — only the helper does.

## Related

- `docs/runbooks/developer-id-signing-validation.md` — the TCC-persistence gate; its
  build/launch loop is reused for Step 4.
- `docs/plans/2026-07-08-001-feat-shared-keychain-access-group-auth-plan.md` — SCR-241,
  which introduced the entitlement and the U6 spike that deferred this check.
- `src/screencap/keychain_group.py`, `src/screencap/auth.py` — the entitled-vs-fallback
  keychain code paths this authorizes.
