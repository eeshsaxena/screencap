---
title: E2EE Beta Toggle Cloud-Capability Gate - Plan
type: fix
date: 2026-07-12
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-plan-bootstrap
execution: code
---

# E2EE Beta Toggle Cloud-Capability Gate - Plan

## Goal Capsule

- **Objective:** Stop the Privacy pane's "End-to-end encryption for shared copies" toggle from being enableable by a signed-out or non-cloud-plan user. Gate the off→on enable path (the only path that mints an iCloud-Keychain KEK) on cloud-capability, and show honest state-specific copy when gated. Fixes SCR-260.
- **Authority hierarchy:** This document's Product Contract. The shipped SCR-220 design (`docs/plans/2026-07-11-002-feat-scr-220-e2ee-shared-copies-plan.md`) is the source of truth for the E2EE row's existing honesty-gate and copy rules; `SECURITY.md` governs trust-claim wording. This plan tightens the row's interactivity without changing any E2EE claim string that is already true.
- **Execution profile:** Two units in dependency order. U1 (pure decision layer + tests) gates U2 (view wiring). The gate logic lands test-first in the policy layer; the view carries no logic of its own.
- **Stop conditions:** Stop and surface if `CloudAuthController` does not expose a usable signed-in / cloud-tier / paywall signal to the Privacy pane's view tree (it does today — see Sources), or if gating the enable path would break an eligible cloud user's shipped SCR-220 flow.
- **Tail ownership:** `ce-work` or a human implementer owns branch/commit/PR. No launch prompt is embedded here.

---

## Product Contract

### Summary

Make the E2EE toggle's off→on enable path aware of the app's existing sign-in and cloud-tier signals. When the user is not cloud-capable, the row becomes non-interactive with copy that names why (signed-out, no cloud plan, or offline/unconfirmed) instead of presenting the enable disclosure and creating a synchronizable key. Eligible cloud users keep the shipped SCR-220 behavior unchanged.

### Problem Frame

Computer-use QA of macOS app v0.12.0 found that on a signed-out, paywall-gated Mac (no recording, no upload possible), the E2EE row is fully interactive: tapping presents the "Turn on end-to-end encryption for cloud copies?" disclosure, and confirming creates the KEK and sets the flag (SCR-260).

The row's decision layer (`PrivacySettingsPolicy.e2eeTapOutcome`) keys only on whether the CLI reports the `cloud_e2ee_enabled` flag at all (`nil` → locked, `false` → show disclosure, `true` → disable) — never on auth or plan state. That was correct for the SCR-220 honesty gate, whose job was binding *copy claims* to the runtime flag, but it left the *interactivity* ungated.

Two harms follow. The row promises "cloud copies upload without end-to-end encryption / turn on to encrypt future uploads" to a user who cannot upload at all — confusing state coupling. And confirming from the app creates a synchronizable KEK in iCloud Keychain (multi-device key custody) for an account-less user — real crypto/key-custody side effects with zero cloud benefit.

The SCR-220 design already treats E2EE as inherently a cloud-upload feature (its sibling onboarding plan states that choosing local-only "never triggers encryption-for-upload"). Gating the app toggle is lossless: during the opt-in beta the `cloud_e2ee_enabled` flag defaults off, so the toggle is the only key-minting path a user reaches from the app, and an eligible user keeps that toggle. A signed-in user is not auto-provisioned a key at sign-in during the beta — `login` mints the KEK only when the flag is already on (after a prior opt-in, or once the Stage 3 default flip lands).

Scope note on the harm. This fix gates the app toggle — the SCR-260 surface. It does not gate the daemon's `screencap e2ee enable` verb or the flag-on `login` path, both of which mint the KEK with no auth/plan check, so a signed-out CLI user on the same Mac can still create an account-less KEK. The client gate here is a UX/consent gate, not a system-level key-creation gate; the daemon lease enforces upload, not key creation. Closing the key-custody harm at its source is a separate follow-up (see Scope Boundaries).

### Requirements

**Gate behavior**

- R1. When the E2EE flag is readable and off, an off→on tap from a non-cloud-capable user must neither present the enable disclosure nor trigger a KEK-creating write. The toggle is non-interactive in that state.
- R2. A user is cloud-capable when signed in AND either holds a Cloud-tier subscription or is running a build with the paywall disabled (pre-billing / dev). A signed-out user is never cloud-capable.
- R3. An eligible (cloud-capable) user's E2EE row keeps the shipped SCR-220 behavior: off→on presents the limits disclosure, and confirming creates the key before setting the flag.
- R4. The on→off disable path stays available regardless of cloud-capability, so a user whose plan lapsed after enabling can still turn encryption off (disabling keeps the key, per SCR-220 U2).
- R5. The nil-flag "locked" state (older CLI without the `e2ee` verb) is unchanged and takes precedence over the cloud-capability gate.

**Row copy honesty**

- R6. The gated row shows copy specific to why it is gated — signed-out, signed-in-without-a-cloud-plan, and signed-in-but-plan-unconfirmed (offline) — and never claims current uploads are, or can currently be, encrypted.
- R7. The existing KTD-9 / KD7 honesty gate still holds across every state, including the new gated captions: none of "always on", "we can't watch", "keys stay", "shared · encrypted", or "only this Mac can decrypt" appear.

### Scope Boundaries

**In scope**

- The macOS app Privacy pane: its pure decision layer (`PrivacySettingsPolicy` / `PrivacySettingsCopy`), the view wiring (`PrivacySettingsView`), their unit tests, and a small read-only `isStale` accessor on `AuthStatus` (the one shared-model touch the view needs).

**Deferred to Follow-Up Work**

- Gating KEK creation at its source — the daemon `e2ee enable` verb and the flag-on `login` path — so an account-less KEK cannot be minted via the CLI. This fix closes the app-toggle surface only; the CLI path stays open and is a separate follow-up if the key-custody harm must be closed system-wide.
- The sign-in / upgrade affordance variant (route the gated row into the account or paywall surface, mirroring the "Recording and search need an active subscription" treatment). This plan uses the minimal honest treatment — a non-interactive row plus caption — matching the pane's existing "coming soon" stub rows. A follow-up can add conversion routing.
- An optional `SECURITY.md` note recording that the E2EE opt-in app toggle is now gated to cloud-capable users (posture tightening, not a threat-model change).

**Outside this fix's identity**

- The Python/daemon `e2ee` verb group and the crypto/KEK machinery itself — unchanged. The daemon lease remains the hard *upload* enforcement (it does not gate key creation); this fix is a UX/consent gate on the app toggle.
- The onboarding E2EE flow, Library badges, and any re-encryption of already-uploaded data.

---

## Planning Contract

### Key Technical Decisions

- KTD-1. **The gate lives in the pure decision layer, not the view.** Add an `E2EECloudEligibility` enum and an `e2eeEligibility(...)` function to `PrivacySettingsPolicy`, plus a new `.gated` case on `E2EETapOutcome`. The view reads `auth` signals and passes primitives in; all branching is unit-testable without a render tree. This matches the SCR-220 KTD-9 / KD7 philosophy that keeps the row's rules assertable in `PrivacySettingsPolicyTests`.
- KTD-2. **The eligibility predicate accounts for the paywall-off build.** Cloud-capable is `signedIn && (isSubscribed || !paywallEnabled)`. Without the paywall-off branch, a pre-billing or dev build — where `tier == .none` for everyone — would gate E2EE for all signed-in users, a regression. This mirrors `CloudAuthController.isGatedForLapse`, which also scopes its gate on `paywallEnabled` and goes dark pre-billing.
- KTD-3. **Fail-closed on the offline/stale case, with its own soft copy.** A signed-in but offline (`stale`) user has no positively-resolved tier. Rather than show them "you need a Cloud plan" (wrong for an offline payer) or optimistically let them mint a KEK, the gate treats stale as a distinct `planUnconfirmed` eligibility with "reconnect to confirm your plan" copy. This honors the billing plan's offline-grace rule (never falsely tell an offline payer they are not entitled) that `CloudAuthController.isGatedForLapse` already embodies, while staying conservative about the key-creating action. Alternative considered — fold `stale` into `noCloudPlan` (one fewer caption) — rejected because it would show an offline Cloud payer an upgrade prompt.
- KTD-4. **Only the off→on (`showDisclosure`) path is gated.** `.locked` (nil flag) and `.disable` (on→off) stay eligibility-independent: the disclosure and KEK creation are the only place the account-less side effect occurs, so they are the only place the gate applies. Turning encryption *off* is always harmless and stays available (R4).
- KTD-5. **The flag-on (`true`) state keeps the unchanged on-copy for every eligibility.** In the rare flag-on-but-ineligible cell (a user who enabled E2EE while subscribed, then signed out or lapsed), the on-copy ("On — new cloud copies from this Mac are encrypted…") is left as-is rather than gaining a fourth gated caption: it becomes true again once uploads resume, the cell is rare, and the `.disable` path (R4) is the escape valve. The gate targets the *enable* path, so this cell is outside the harm surface. The honesty analysis is scoped this way deliberately, not by omission.

### High-Level Technical Design

The change is a decision matrix in the policy layer. Two tables define it.

**Eligibility derivation** — `e2eeEligibility(isSignedIn, isSubscribed, stale, paywallEnabled)`:

| isSignedIn | paywallEnabled | isSubscribed (Cloud tier) | stale | → eligibility |
|---|---|---|---|---|
| false | any | any | any | `signedOut` |
| true | false | any | any | `eligible` |
| true | true | true | any | `eligible` |
| true | true | false | true | `planUnconfirmed` |
| true | true | false | false | `noCloudPlan` |

**Row resolution** — flag state × eligibility → tap outcome, interactivity, caption:

| cloudE2EEEnabled | eligibility | tap outcome | interactive? | caption |
|---|---|---|---|---|
| `nil` | any | `locked` | no | stub ("Not available yet…") |
| `true` | any | `disable` | yes (on→off) | on-copy (unchanged) |
| `false` | `eligible` | `showDisclosure` | yes (off→on → disclosure) | off-copy (unchanged) |
| `false` | `signedOut` | `gated` | no | gated: sign-in copy |
| `false` | `noCloudPlan` | `gated` | no | gated: cloud-plan copy |
| `false` | `planUnconfirmed` | `gated` | no | gated: reconnect copy |

The view treats `gated` exactly like `locked` for rendering (non-interactive toggle, dimmed row); only the caption and the tap-handler no-op differ.

### Assumptions

- `CloudAuthController` (injected as the `auth` environment object at the app root and already consumed by `MainWindow`) exposes current `isSignedIn`, `isSubscribed`, `tier`, `paywallEnabled`, and an `AuthStatus` carrying the `stale` bit — verified in Sources. The Privacy pane can add `@EnvironmentObject var auth` and read them.
- `isSubscribed` is the app's derived Cloud-capability signal (`tier == .cloud`, with a legacy `subscribed` fallback), and is true during a Cloud trial. It is the right positive signal for "can upload to cloud."
- The pane reads already-resolved published auth state. Any refresh uses `CloudAuthController.refreshIfNeeded()` (the coalesced, no-op-once-resolved variant every other cloud surface uses), never eager `refresh()` — eager `whoami` on a pane `.task` re-decrypts the Keychain and re-raises the SCR-241 authorization prompt. A subscribe-while-the-pane-is-open flow un-gates reactively (the row rebinds on `auth`'s `@Published tier`) plus the app-activation entitlement re-check after a Stripe return; the pane does not need eager per-open refresh.

### Sequencing

U1 (decision layer + tests) has no dependencies and lands first. U2 (view wiring) depends on U1's new signatures and enum.

---

## Implementation Units

### U1. Cloud-capability eligibility and gated tap outcome in the decision layer

- **Goal:** Add the eligibility predicate, the `.gated` tap outcome, and the gated captions to the pure policy layer, fully unit-tested, and thread the new `e2eeTapOutcome` signature through existing call sites so the build stays green.
- **Requirements:** R1, R2, R3, R4, R5, R6, R7
- **Dependencies:** none
- **Files:**
  - `macos/Screencap/Views/Settings/PrivacySettingsPolicy.swift` — add `E2EECloudEligibility` enum and `e2eeEligibility(...)`; add `.gated` to `E2EETapOutcome`; extend `e2eeTapOutcome` to take eligibility; add the three gated caption strings to `PrivacySettingsCopy` and extend `e2eeCaption(...)` to key on eligibility; extend `e2eeHelp(...)` so the gated states return gated help (not the off-state "turn on to encrypt" hover — otherwise the tooltip contradicts the gated caption); extend the honesty-sweep helper (`allRowStrings`) to include the gated captions and gated help. `e2eeChip` stays "beta" for the readable-flag states (the row is gated, not absent); the view carries the non-interactive chip treatment (see U2).
  - `macos/ScreencapTests/PrivacySettingsPolicyTests.swift` — new eligibility, gated-outcome, and gated-caption tests; update the existing `e2eeTapOutcome` and `e2eeCaption`/`e2eeHelp` call sites (the nil/false/true state tests) for the new signatures. Giving the new `eligibility` parameter a default of `.eligible` keeps the flag-only call sites compiling while the view passes it explicitly — decide default-vs-explicit-update at implementation.
  - `macos/ScreencapTests/PrivacyControllerTests.swift` — update the two `e2eeTapOutcome` call sites (around lines 605 and 627) for the new signature.
- **Approach:**
  - `e2eeEligibility` takes primitives (`isSignedIn`, `isSubscribed`, `stale`, `paywallEnabled`) and returns the enum per the KTD-2/KTD-3 derivation table — no dependency on `EntitlementTier`, so it is trivially testable.
  - `e2eeTapOutcome(cloudE2EEEnabled:eligibility:)` returns `.locked` for `nil` and `.disable` for `true` regardless of eligibility (KTD-4); for `false` it returns `.showDisclosure` when eligibility is `eligible`, else `.gated`.
  - `e2eeCaption(cloudE2EEEnabled:eligibility:)` returns the stub for `nil`, the on-copy for `true`, and for `false` returns the existing off-copy when `eligible`, else the gated caption for that eligibility. Pin these exact gated strings (they name the reason, make no current-encryption claim, and avoid "not available" — the capability exists, it is gated):
    - `signedOut`: "Cloud copies need a cloud plan. Sign in with a Cloud subscription to encrypt future uploads from this Mac."
    - `noCloudPlan`: "Cloud copies need a cloud plan. Encrypting future uploads is available on the Cloud subscription."
    - `planUnconfirmed`: "Couldn't confirm your plan while offline. Reconnect to turn on encryption for cloud copies."
  - The gated `e2eeHelp` string (one constant is enough): "Encrypting cloud copies is available with a cloud plan."
  - The `e2eeTapOutcome` signature change is mechanical at the `.locked` / `.disable` sites (they pass any eligibility); the `.showDisclosure` site passes `.eligible`.
- **Execution note:** Pure functions — land test-first. This gate is exactly the kind of decision the policy tests exist to pin.
- **Technical design:** See the two tables in High-Level Technical Design; they are the authoritative spec for this unit's branching.
- **Patterns to follow:** the existing state-keyed `e2eeTapOutcome` / `e2eeCaption`; `keepLocalCaption`'s switch-per-state shape; the `allRowStrings` honesty sweep and its `testRowCopyCarriesNoUntrueClaims` consumer.
- **Test scenarios:**
  - Eligibility, signed-out: `isSignedIn=false` → `signedOut` for every combination of the other inputs.
  - Eligibility, paywall off: `isSignedIn=true, paywallEnabled=false` → `eligible` regardless of `isSubscribed`/`stale` (KTD-2 pre-billing guard).
  - Eligibility, paywall on + subscribed: `isSignedIn=true, paywallEnabled=true, isSubscribed=true` → `eligible` (including the trial case, which reports subscribed).
  - Eligibility, paywall on + stale: `isSignedIn=true, paywallEnabled=true, isSubscribed=false, stale=true` → `planUnconfirmed` — and `stale` takes precedence over `isSubscribed=false` (offline payer never shown the upgrade prompt).
  - Eligibility, paywall on + not subscribed + not stale → `noCloudPlan`.
  - Tap outcome, nil flag → `.locked` for every eligibility (older-CLI precedence, R5).
  - Tap outcome, true flag → `.disable` for every eligibility, including `noCloudPlan` (a since-lapsed user can still turn it off, R4).
  - Tap outcome, false flag + `eligible` → `.showDisclosure` (R3, shipped path preserved).
  - Tap outcome, false flag + each of `signedOut` / `noCloudPlan` / `planUnconfirmed` → `.gated` (R1).
  - Gated caption content: the `signedOut` caption references signing in with a cloud plan; the `noCloudPlan` caption references a Cloud plan; the `planUnconfirmed` caption references reconnecting. None contains "is encrypted" or "not available yet" (the capability exists; it is gated, not absent). (Covers R6.)
  - Honesty sweep extended: for every gated caption and the gated help string, the forbidden strings ("always on", "we can't watch", "keys stay", "shared · encrypted", "only this mac can decrypt", "is encrypted") are absent. (Covers R7.)
- **Verification:** `PrivacySettingsPolicyTests` and `PrivacyControllerTests` compile and pass; the honesty sweep now covers the gated captions; no existing E2EE claim-string test regresses.

### U2. Wire the cloud-capability gate into the Privacy pane view

- **Goal:** Read `auth` in `PrivacySettingsView`, compute eligibility, render the `.gated` state non-interactively with the gated caption, and no-op the tap handler so a gated tap never fires a write.
- **Requirements:** R1, R3, R4, R5, R6
- **Dependencies:** U1
- **Files:**
  - `macos/Screencap/Views/Settings/PrivacySettingsView.swift` — add `@EnvironmentObject private var auth: CloudAuthController`; in `e2eeRow` / `e2eeRowHeader` compute `eligibility` from `auth.isSignedIn`, `auth.isSubscribed`, `auth.status.isStale`, and `auth.paywallEnabled`, and pass it into `e2eeTapOutcome`, `e2eeCaption`, and `e2eeHelp`; treat the non-interactive predicate as `outcome == .locked || outcome == .gated` (dim + `action: nil`) and widen the chip-color local so a `.gated` chip renders in the muted (locked-like) treatment; add `.gated` to the `toggleE2EE()` switch as a `return`; call `auth.refreshIfNeeded()` in the pane's `.task` (the SCR-241-safe coalesced refresh, never eager `refresh()`).
  - `macos/Screencap/Models/AuthStatus.swift` — add a `var isStale: Bool` accessor so the view can derive the `stale` primitive without pattern-matching inline.
- **Approach:**
  - Compute one `eligibility` value in `e2eeRowHeader` and reuse it for the outcome, the toggle interactivity, and the caption — a single source so the row can't show inconsistent state.
  - Preserve the existing Swift type-checker workaround: `e2eeRowHeader` builds `toggleAction` with an explicit `if` and closure literal (never a ternary between `nil` and a bare method reference — that crashed the x86_64 Release solver). Extend the same `if` to cover the gated case.
  - Use `await auth.refreshIfNeeded()` (not eager `refresh()`) in the pane `.task`; the gate otherwise renders from the already-published `auth` state, and a subscribe-while-open flow un-gates reactively via `auth`'s `@Published tier` plus the app-activation entitlement re-check. The initial `.unknown` auth state is gated (safe default) for the tap outcome, but map `.unknown` to a neutral/loading caption rather than the `signedOut` "sign in" copy, so an already-eligible user doesn't see a sign-in prompt flash during the first `whoami` round-trip.
- **Execution note:** View wiring plus a refresh dependency; the decision logic is already proven in U1. Verify by building and walking the state matrix below (the QA path that found the bug), not by a new unit test.
- **Patterns to follow:** the existing `e2eeRow` locked-state handling (opacity `0.75`, `action: nil`, `.help(...)`); the `pauseRow` stub as the reference look for a dimmed non-interactive row; `CloudAuthController.refreshIfNeeded()` as used by the other cloud surfaces (menu-bar account section, `AccountSheetView`, `ReviewWindow`, `OnboardingStorageSteps`) — the SCR-241-safe on-appear refresh, not eager `refresh()`.
- **Test scenarios:** `Test expectation: none — view wiring only; the gate's branching is unit-tested in U1, and this codebase proves SwiftUI panes through the policy layer plus manual QA, not view snapshots.` Behavior is verified via the manual state matrix in the Verification Contract.
- **Verification:** app builds; the manual state matrix passes — signed-out reproduces as gated (SCR-260 fixed), an eligible Cloud user still gets the disclosure, and the older-CLI locked row is unchanged.

---

## Verification Contract

| Gate | What it proves | Applies to |
|---|---|---|
| `ScreencapTests` — `PrivacySettingsPolicyTests` + `PrivacyControllerTests` | Eligibility matrix, gated tap outcome, gated-caption honesty, unchanged shipped paths | U1 |
| macOS app build (XcodeGen project) | View wiring compiles; no Swift type-checker regression on `e2eeRowHeader` | U2 |
| Manual state matrix (below) | The user-visible gate behaves across auth/plan states | U2 |

- **Test target:** the Swift tests run in the macOS `ScreencapTests` target of the XcodeGen-generated project, not the Python `pytest -m privacy` CI lane. Run them via the app's build/test flow. In this worktree under `~/Documents`, use the documented compile/test-on-a-`/private/tmp`-copy workaround — do not launch `xcodebuild` against the worktree (it TCC-bricks the session). See the worktree build/test solution notes.
- **Manual state matrix (U2):**
  - Signed out (`whoami` → `signed_in: false`), paywall on → E2EE row non-interactive, "sign in with a cloud plan" caption; tapping does nothing (SCR-260 repro is now fixed).
  - Signed in, no Cloud plan (`tier` local/none), paywall on → non-interactive, "Cloud plan" caption.
  - Signed in, offline/stale (`planUnconfirmed`) → non-interactive, "reconnect to confirm your plan" caption (not an "upgrade" prompt).
  - Signed in, Cloud plan (or Cloud trial) → off→on presents the SCR-220 disclosure; confirming creates the key and flips the flag (shipped path intact).
  - Signed in, Cloud plan, then lapsed while flag is on → row shows on; toggling off still works (R4).
  - Older CLI (nil flag) → locked stub row unchanged (R5).
  - Paywall-off / dev build, signed in → eligible; off→on presents the disclosure (no regression, KTD-2).

---

## Definition of Done

**Global**

- The E2EE off→on enable path is non-interactive for signed-out and non-cloud-plan users; no disclosure and no KEK-creating write fires in those states (R1, R2).
- Eligible cloud users' shipped SCR-220 behavior is unchanged (R3); the disable path and older-CLI locked state are unchanged (R4, R5).
- Gated captions are honest and state-specific; the KTD-9 / KD7 honesty sweep passes over the new captions (R6, R7).
- All gate branching lives in the policy layer and is unit-tested; the view carries no decision logic.
- `PrivacySettingsPolicyTests` and `PrivacyControllerTests` pass; the app builds; the manual state matrix passes.
- No dead code or abandoned scaffolding left in the diff.

**Per unit**

- U1: eligibility + gated outcome + gated captions implemented; all listed test scenarios pass; existing call sites updated; honesty sweep extended.
- U2: `auth` wired into the pane; `.gated` renders non-interactively with the correct caption; tap handler no-ops on `.gated`; pane refreshes auth on open; manual matrix passes.

---

## Sources & Research

- **SCR-260** (this fix): the QA finding and its two proposed directions — gate, or caption-only. This plan takes the gate direction (chosen with the user).
- **SCR-220 shared-copies plan** — `docs/plans/2026-07-11-002-feat-scr-220-e2ee-shared-copies-plan.md`: the E2EE row's honesty-gate design (KTD-9 / KD7), the `e2eeTapOutcome` / `e2eeCaption` state machine, and the `login`-creates-KEK-when-flag-on behavior that makes gating lossless.
- **E2EE cloud onboarding plan** — `docs/plans/2026-07-06-002-feat-e2ee-cloud-onboarding-plan.md`: establishes E2EE as a cloud-upload feature (local-only "never triggers encryption-for-upload").
- **Decision layer** — `macos/Screencap/Views/Settings/PrivacySettingsPolicy.swift`: `e2eeTapOutcome` / `e2eeToggleOn` / `e2eeCaption` / `e2eeChip` and `PrivacySettingsCopy.allRowStrings` (the honesty sweep).
- **View** — `macos/Screencap/Views/Settings/PrivacySettingsView.swift`: `e2eeRow` / `e2eeRowHeader` (including the x86_64 type-checker workaround), `toggleE2EE`, and the `.task` refresh hook.
- **Auth signals** — `macos/Screencap/Controllers/CloudAuthController.swift`: `isSignedIn`, `isSubscribed`, `tier`, `paywallEnabled`, `isGatedForLapse` (the paywall-scoped gate this fix mirrors), and `refreshIfNeeded()` (the SCR-241-safe coalesced refresh the pane must use — eager `refresh()` re-raises the Keychain authorization prompt). `macos/Screencap/Models/AuthStatus.swift`: `AuthStatus` (`stale` bit) and `EntitlementTier`.
- **KEK-creation paths not gated by this fix** — the CLI `e2ee enable` verb and the flag-on `login` path both call `get_or_create_cloud_kek()` (`src/screencap/cloud_crypto.py`) with no auth/plan check; `get_cloud_e2ee_enabled` defaults the flag off during the beta. Grounds the Problem Frame's scope note and the deferred CLI-gating follow-up.
- **Wiring** — `macos/Screencap/ScreencapApp.swift` injects `auth` at the app root; `macos/Screencap/Views/MainWindow.swift` already consumes it, so the Privacy pane can read it via `@EnvironmentObject`.
- **Tests** — `macos/ScreencapTests/PrivacySettingsPolicyTests.swift` (pure policy + honesty gate) and `macos/ScreencapTests/PrivacyControllerTests.swift` (`e2eeTapOutcome` call sites at ~605 and ~627).
