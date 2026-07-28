---
title: Launch Price Update — $9 Local Pro / $20 Cloud - Plan
type: feat
date: 2026-07-28
topic: launch-price-update
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-plan-bootstrap
execution: code
---

# Launch Price Update — $9 Local Pro / $20 Cloud - Plan

## Goal Capsule

- **Objective:** Set the app's displayed launch prices to **$9/month Local Pro** and **$20/month Cloud**, and retire the last user-facing "free · forever" claim so no build advertises a free tier that does not exist.
- **Product authority:** This document. It supersedes the indicative `~$8 / ~$15` in `docs/plans/2026-07-10-001-feat-paid-only-launch-pricing-plan.md` (R10 explicitly marked those placeholders "to be finalized before launch") and supplies the finalized numbers that `docs/plans/2026-07-20-002-feat-stripe-live-charging-cutover-plan.md` U1 was waiting on.
- **Execution profile:** Small and copy-only. The paid-only two-tier machinery already shipped: prices flow from one constant (`PricingCatalog`) into every priced surface, so this is 2 units touching 2 source files plus their tests. **No entitlement, gating, checkout, or billing logic changes.**
- **Stop conditions:** Stop and surface if the live Stripe prices cannot be moved to $9/$20 before the next DMG ships (the displayed price is compiled into the app and would then lie), or if a surface is found that would display a price while `paywallEnabled` is off.
- **Open blockers:** None in-repo. One **operational prerequisite** outside this diff: the Stripe prices must be updated to match before the go-live DMG — tracked in Definition of Done.

---

## Product Contract

### Summary

The paid-only launch (no free tier, Local Pro + Cloud, each fronted by a card-required free trial) is already built and shipped dark behind default-off flags. What is stale is the *numbers* — the app compiles in `$8` and `$15`, which were always placeholders — and one line of pre-billing copy that still advertises the local tier as "free · forever · no account". This change finalizes the prices to $9 and $20 and removes the free-tier claim.

### Problem Frame

Two things are currently untrue in the shipped app:

1. **The prices are placeholders.** `PricingCatalog` carries `$8` / `$15`, labeled in-code as the finalized launch prices, pinned by an exact-value test. The commercial decision is now $9 and $20.
2. **A free-tier claim survives in the paywall-off build.** `OnboardingCopy.localCardMeta = "free · forever · no account"` renders on the storage step whenever `auth.paywallEnabled` is false — the state the shared screenshot shows. There is no free tier in the product, so "forever" is a promise the business will not keep, and it is exactly the retroactive-removal trap the paid-only plan's Key Decisions called out.

The fix is narrow because the architecture already anticipated it: KTD-7 of the paid-only plan made price a single constant precisely so "tuning a price is a one-line change, never a hunt for duplicated `$`-literals."

### Key Decisions

- **KTD1. Local Pro is $9/month, Cloud is $20/month.** *(session-settled: user-directed — chosen over the placeholder ~$8 / ~$15: the user specified these prices directly.)* Governs R1, R2. Monthly is not a new choice — it is what the shipped implementation already assumes (`"\(price)/month"`, Stripe *recurring* prices, `TRIAL_PERIOD_DAYS`); this plan reads that period off the code rather than introducing one.
- **KTD2. No free tier is advertised anywhere in the app.** *(session-settled: user-directed — chosen over keeping the "free · forever · no account" local card: the product is paid-only.)* Governs R3.
- **KTD3. The paywall-off card meta drops the free claim without adding a price claim.** The paywall-off branch exists (KTD-6 of the paid-only plan) so the app never advertises a price it is not enforcing. Replacing "free · forever · no account" with a price would invert that dishonesty. The replacement states a true, unpriced fact about the pre-billing build: **`"no account needed"`**. Governs R3, R4.
- **KTD4. Display only — enforcement is untouched.** No change to `paywallEnabled`, `SCREENCAP_STRIPE_PAYWALL`, `SCREENCAP_LOCAL_PAYWALL_ENFORCE`, the entitlement claim, the signer gate, or checkout. Those flips belong to the Stripe live-charging cutover, not to a price edit. Governs R5.
- **KTD5. Stripe stays the billed truth; the app's number must be made to match it out-of-band.** The displayed price ships compiled in the DMG; the charged price lives in Stripe price objects referenced by `STRIPE_PRICE_ID_LOCAL` / `STRIPE_PRICE_ID_CLOUD`. Nothing in this repo can move the charged price. Governs R6.

### Actors

- A1. Prospective user — sees the storage-step cards and the account sheet's plan/upgrade copy, and decides whether to start a trial.
- A2. Operator — updates the live Stripe price objects so the charged amount matches what the DMG displays.

### Requirements

**Prices**

- R1. Local Pro displays as **$9/month** on every priced surface.
- R2. Cloud displays as **$20/month** on every priced surface, and remains priced above Local Pro.
- R3. No user-facing string in the app claims a free tier, a free-forever local plan, or a permanently no-cost path. "Free trial" copy is unaffected — the trial is real.

**Invariants preserved**

- R4. With `paywallEnabled` off, the app still makes **no pricing or billing claim** it is not enforcing.
- R5. Entitlement, gating, checkout, and trial-lifecycle behavior are unchanged by this plan.
- R6. The displayed prices match the live Stripe prices before the next DMG reaches users.

### Key Flows

- F1. First-run storage choice (paywall on)
  - **Trigger:** A new user reaches onboarding step 3 with `paywallEnabled == true`.
  - **Actors:** A1
  - **Steps:** Cards render as Local Pro "$9/month · free trial", Cloud "$20/month · free trial", Team as coming-soon → the trial disclosure renders beneath → user picks a tier.
  - **Covered by:** R1, R2, R3

- F2. First-run storage choice (paywall off — the pre-billing/dev build)
  - **Trigger:** Same step with `paywallEnabled == false` (the state in the shared screenshot).
  - **Actors:** A1
  - **Steps:** Cards render with unpriced pre-billing metas; the local card no longer says "free · forever" → no price, no trial disclosure, no free-tier promise.
  - **Covered by:** R3, R4

### Acceptance Examples

- AE1. **Covers R1, R2.** **Given** the paywall is on, **when** the storage step renders, **then** the Local Pro card meta contains `$9` and the Cloud card meta contains `$20`.
- AE2. **Covers R1, R2.** **Given** any priced surface composed from `PricingCatalog` (storage cards, account-sheet tier buttons, plan price line), **when** it renders, **then** it shows the same two numbers — no surface carries its own literal.
- AE3. **Covers R3.** **Given** the full audited storage/account copy bundle, **when** it is scanned, **then** it contains no "forever" claim.
- AE4. **Covers R4.** **Given** the paywall is off, **when** the storage step renders, **then** no card meta contains a `$` amount and no trial disclosure appears.
- AE5. **Covers R5.** **Given** this change, **when** the diff is reviewed, **then** it touches only copy/constants and their tests — no gating, entitlement, or checkout code.

### Copy-by-flag matrix

The whole change lives in two cells of this matrix. The free-tier claim only ever rendered in the paywall-off row, which is why it survived the paid-only launch.

| Card | `paywallEnabled == false` | `paywallEnabled == true` |
|---|---|---|
| Local | `localCardTitle` "This Mac only" / meta ~~`"free · forever · no account"`~~ → **`"no account needed"`** | `localProCardTitle` "Local Pro" / meta **`$9/month · free trial`** |
| Cloud | `personalCardTitle` "Personal cloud" / meta `"just you"` *(unchanged)* | `cloudCardTitle` "Cloud" / meta **`$20/month · free trial`**, or "Upgrade — add cloud" for a Local Pro holder |
| Team | "Team cloud" / "you + your team" *(unchanged, coming-soon, never priced)* | same |

---

## Planning Contract

### Scope Boundaries

**In scope**
- The two `PricingCatalog` price constants and the doc comment that names their values.
- The paywall-off local card meta string and its guard test.
- The tests that pin the above.

**Non-goals**
- Flipping any paywall or enforcement flag. Owned by `docs/plans/2026-07-20-002-feat-stripe-live-charging-cutover-plan.md`.
- Changing entitlement, checkout, webhook, signer, or trial-lifecycle logic.
- The Team tier's pricing — it stays an unpriced coming-soon card.
- Grandfathering rules for existing subscribers. The paid-only plan's R11 already covers this and is unaffected by a display-number change.
- Marketing/website copy — `screencap.sh` lives in the separate `screencap-website` repo.

**Deferred to follow-up work**
- Sourcing prices from the server (`whoami`/config) instead of compiled constants, so display cannot drift from Stripe without a rebuild. Already recorded as `TODO(build-verify)` in `PricingCatalog`; this change makes the drift risk concrete but does not fix it.
- Renaming `personalCardMetaFree` — the `…Free` suffix means "paywall-off variant", not "free tier", and now reads as a contradiction. Cosmetic; not requested.

### Assumptions

- **Billing period is monthly.** Read from the existing implementation (`/month` price lines, Stripe recurring prices, trial-days env), not chosen here. If the intent was annual or one-time, this plan is wrong and needs revisiting.
- **The Stripe price objects will be updated to $9/$20 by the operator.** This plan cannot verify or perform that; it is a DoD gate, not a code unit.
- **$20 Cloud is intentional at the top of the category band.** The paid-only plan anchored the recall-tool band at ~$19–20; $20 sits at that ceiling rather than below it. Recorded because it is a deliberate position, not a typo.

### Open Questions

- Does the live Stripe account already carry $8/$15 price objects that need editing, or are the live-mode prices still unprovisioned (in which case they should simply be created at $9/$20)? Answer determines whether existing subscribers exist to grandfather. Resolve with the operator before the go-live DMG; it does not block this diff.

---

## Implementation Units

### U1. Finalize the displayed launch prices at $9 / $20

- **Goal:** `PricingCatalog` publishes the finalized launch prices, so every priced surface updates from one edit.
- **Requirements:** R1, R2 (per KTD1, KTD5).
- **Dependencies:** none.
- **Files:**
  - `macos/Screencap/Views/Onboarding/OnboardingStepPolicy.swift` — `PricingCatalog.localProMonthly`, `PricingCatalog.cloudMonthly`, and the enum's doc comment that names "(Local Pro $8, Cloud $15)".
  - `macos/ScreencapTests/OnboardingStepPolicyTests.swift` — `testFinalizedLaunchPricesArePinned` and its docstring.
- **Approach:**
  1. Set `localProMonthly` to `"$9"` and `cloudMonthly` to `"$20"`.
  2. Update the `PricingCatalog` doc comment so the values it names match the constants — the comment currently asserts the finalized prices are $8/$15 and would become a false in-code claim. Leave the `TODO(build-verify)` note about server-sourced prices intact.
  3. Update the exact-value assertions in `testFinalizedLaunchPricesArePinned` to `"$9"` / `"$20"`, and its docstring's parenthetical.
  4. Change nothing else. `localProPriceLine` / `cloudPriceLine`, the card metas, `AccountSheetCopy.tierButtonTitle`, `tierDetail`, and `planPriceLine` all compose from these constants and need no edit — that is the KTD-7 contract this unit is exercising.
- **Execution note:** The pinning test is an intentional tripwire, not an obstacle — change the constant and the pin in the same commit, and never relax the assertion to a looser match.
- **Patterns to follow:** the existing `PricingCatalog` shape and the single-source composition already used by `AccountSheetPolicy`.
- **Test scenarios:**
  - `PricingCatalog.localProMonthly == "$9"` and `cloudMonthly == "$20"` (the updated pin).
  - Covers AE1. `OnboardingCopy.localProCardMeta` contains `$9`; `OnboardingCopy.cloudCardMeta` contains `$20` — already asserted by `testTwoTierCardsSourcePriceFromCatalog` via the catalog reference; confirm it still passes unmodified, which is the evidence that no surface hardcodes a literal.
  - Covers AE2. `AccountSheetCopy.tierButtonTitle(.localPro)` contains `$9` and `.cloud` contains `$20`, proving the account sheet inherits the change without its own edit.
  - Edge: Cloud's price remains strictly greater than Local Pro's (R2), asserted **numerically** — strip the leading `$` and compare as `Int`. A naive string comparison is wrong for exactly this pair: `"$20" < "$9"` lexicographically, so a `>` on the raw strings would fail on correct data.
  - The trial-honesty assertions (`testPaidCardsAndTrialCopyAreHonest`) still pass — the price edit introduced no forbidden claim.
- **Verification:** The macOS app test target passes with no edits to any test other than the price pin; a repo-wide search for `"$8"` / `"$15"` finds no surviving user-facing price literal.

### U2. Retire the "free · forever" claim from the paywall-off local card

- **Goal:** No build advertises a free-forever local tier, while the paywall-off branch keeps making no pricing claim.
- **Requirements:** R3, R4 (per KTD2, KTD3).
- **Dependencies:** none (independent of U1; sequence either way).
- **Files:**
  - `macos/Screencap/Views/Onboarding/OnboardingStepPolicy.swift` — `OnboardingCopy.localCardMeta` (the "free · forever · no account" string) and the surrounding comment block describing the step-3 storage copy.
  - `macos/ScreencapTests/OnboardingStepPolicyTests.swift` — `testStorageAndAccountCopyCarriesNoForbiddenClaims`.
- **Approach:**
  1. Replace `localCardMeta` with `"no account needed"` — true in the pre-billing build, and free of both the permanence promise and any price claim (KTD3).
  2. Update the adjacent comment, which currently says the local card's design copy "is true and ships as-is"; that statement is what went stale.
  3. Extend the existing forbidden-claims list in `testStorageAndAccountCopyCarriesNoForbiddenClaims` with `"forever"`. This is the right guard word: it appears nowhere else in `storageAndAccountStrings`, and unlike banning `"free"` it does not collide with the legitimate "free trial" copy on the paid cards.
  4. Leave `personalCardMetaFree` ("just you") and the Team card alone — neither claims a price or a free tier.
- **Patterns to follow:** the existing forbidden-substring audit in the same test file (the `["encrypt", "e2e", "keys stay", "we can't watch"]` list) — this is the established mechanism for pinning honest copy in this codebase.
- **Test scenarios:**
  - Covers AE3. The joined, lowercased `storageAndAccountStrings` bundle contains no `"forever"`.
  - The bundle still contains `"free trial"` via the paid card metas — proving the new guard bans the false claim without banning the true one.
  - Covers AE4. With the paywall off, the rendered local and cloud card metas (`localCardMeta`, `personalCardMetaFree`) contain no `$` character — a regression guard on KTD-6's no-unenforced-price-claim invariant.
  - `localCardMeta` is non-empty and still describes the local tier (guards against silently deleting the meta line and leaving a bare card).
- **Verification:** The macOS app test target passes; a repo-wide search for "free · forever" and "forever" across user-facing Swift copy returns nothing.

---

## Verification Contract

- **CI cannot verify this diff — it must be verified locally.** `.github/workflows/ci.yml` runs only Python lanes (privacy guards macOS/Linux, lock-policy contract, cloud-function billing); there is no Swift or `xcodebuild` job. Every test this plan touches is Swift, so a green CI run is **not** evidence that the change is correct. Treat the local run as the gate.
- The macOS app test target builds and passes: `xcodebuild test` against the `Screencap` scheme (the known-flaky daemon-reconnect test is unrelated to this diff and is not a signal here).
- Both `OnboardingStepPolicyTests` and `AccountSheetPolicyTests` pass, since both consume `PricingCatalog`.
- No Python lane is implicated — this diff touches no Python.
- **AE5** is verified at review time, not by a test: the diff touches only copy constants and their tests, with no change to gating, entitlement, checkout, or flag-resolution code.
- Manual visual check is **optional**, not required: both states are string-assertable and already pinned by policy tests, which is why this codebase keeps copy in `OnboardingCopy` rather than inline in views.

## Definition of Done

1. `PricingCatalog` publishes `$9` (Local Pro) and `$20` (Cloud), with its doc comment matching.
2. No user-facing string in the app claims a free tier or a "forever" free local plan; a test enforces this.
3. Every priced surface — storage cards, account-sheet tier buttons, plan price line, upgrade panel — shows the new numbers without any surface-local edit, proving the single-source contract held.
4. Paywall-off rendering still displays no price and no trial disclosure.
5. The macOS app test target passes **in a local run** — CI has no Swift job and will go green regardless.
6. **Operational gate (outside this diff, blocks the go-live DMG, not the PR):** the live Stripe prices for Local Pro and Cloud read $9 and $20 before a DMG carrying these strings reaches users. Until then the app must not be released with enforcement on. Owner: A2 / the Stripe live-charging cutover runbook.

---

## Risks & Dependencies

- **Displayed price drifting from the charged price.** The number ships compiled in the DMG; Stripe holds the charged amount. A DMG released before the Stripe prices move would show $9/$20 and charge $8/$15. *Mitigation:* DoD item 6 gates the release, and the enforcement flags stay off until the cutover — this is the same coupling KTD-4 of the cutover plan already governs.
- **Existing subscribers at the old prices.** If live-mode subscriptions already exist at $8/$15, changing the display does not migrate them. *Mitigation:* the paid-only plan's R11 grandfathering already covers this; the Open Question above resolves whether any such subscribers exist.
- **Under-scoping the "no free tier" instruction.** This plan treats it as a copy correction, because the product-level paid-only decision already shipped and the free claim only renders in the paywall-off build. If the intent was instead *"turn the paywall on now"*, that is the Stripe live-charging cutover, not this diff — surface it rather than expanding scope here.

## Sources & Research

- `macos/Screencap/Views/Onboarding/OnboardingStepPolicy.swift` — `PricingCatalog` (the single price source) and all onboarding storage copy.
- `macos/Screencap/Views/Onboarding/OnboardingStorageSteps.swift` — the `paywallEnabled` branch that selects paid vs. pre-billing copy per card.
- `macos/Screencap/Views/Account/AccountSheetPolicy.swift` — downstream price consumers (`tierButtonTitle`, `planPriceLine`).
- `macos/ScreencapTests/OnboardingStepPolicyTests.swift` — the exact-value price pin and the honest-copy forbidden-substring audit.
- `docs/plans/2026-07-10-001-feat-paid-only-launch-pricing-plan.md` — origin of the paid-only, two-tier, trial-fronted model; R10 marked $8/$15 as placeholders to finalize.
- `docs/plans/2026-07-20-002-feat-stripe-live-charging-cutover-plan.md` — U1 "Finalize launch prices in the client", which this plan supplies the numbers for; owns the flag flips and live provisioning.
- `scripts/cloud-function/billing.py` — confirms price→tier mapping is env-driven (`STRIPE_PRICE_ID_LOCAL` / `_CLOUD`), so no repo-side amount exists to change.
