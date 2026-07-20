---
date: 2026-06-03
topic: individual-apple-dev-membership-tester-distribution
---

# Ship notarized macOS tester builds under the individual Apple Developer membership

## Summary

Distribute notarized Screencap tester builds now under the sole developer's individual Apple Developer Program membership, and structure signing so the eventual switch to the org account is a CI-secrets swap plus one planned permission re-grant — not a rebuild, rename, or app transfer.

---

## Problem Frame

The org Apple Developer Program enrollment is still in progress (expected ~2 weeks out), but the macOS app is ready to put in front of a small set of external testers. Today the `.app` has no distribution path: it is dev-only, signed per-developer via the `DEVELOPMENT_TEAM` env var, and falls back to ad-hoc signing that triggers a documented TCC re-grant treadmill on every rebuild. Waiting for the org account to start external testing costs real early-feedback time during the highest-learning window.

There is one genuine cost to starting under the individual account: macOS TCC keys permission grants on `(bundle id, Team ID)`. Screencap depends on the three heaviest grants — Screen Recording, Accessibility, Input Monitoring. When builds are later re-signed with the org's Team ID, every already-installed tester's grants orphan and must be re-granted once. For the current cohort — a handful of friendly testers reachable in Slack — that cost rounds to zero. Everything else about an individual→org switch is cheap because distribution is direct/notarized, the bundle id is stable, and no team-scoped Apple capabilities are in use.

---

## Actors

- A1. Sole developer: builds, signs, and notarizes the app; owns both the individual membership now and the org membership later.
- A2. Tester cohort: a small, friendly, Slack-reachable group who install notarized builds and (once) re-grant permissions at the org flip.
- A3. Individual Apple Developer account: the signing/notarization identity used during the org-enrollment window.
- A4. Org Apple Developer account: the permanent signing/notarization identity once enrollment completes.

---

## Key Flows

- F1. Org Team-ID flip (one-time)
  - **Trigger:** Org Apple Developer Program enrollment completes.
  - **Actors:** A1, A2, A4
  - **Steps:** Swap the signing identity + notary credentials from individual to org (config/secrets only); cut one release; post a heads-up in Slack that this update needs a one-time permission re-grant; testers update and re-grant Screen Recording + Accessibility + Input Monitoring via the existing walkthrough.
  - **Outcome:** All tester builds carry the org Team ID; no bundle-id change, no source change, grants restored once.
  - **Covered by:** R4, R5, R6

---

## Requirements

**Distribution under the individual account**
- R1. Tester builds are signed for direct distribution and notarized under the individual Apple Developer Program membership so they launch without Gatekeeper friction.
- R2. The bundle id stays `com.screencap.macos` and does not change across the individual→org switch.
- R3. Distribution stays direct (downloadable notarized build); no Mac App Store / App Store Connect record is opened under the individual account.

**Keep the org migration to a re-sign**
- R4. Signing identity (Team ID), Developer ID certificate, and notary credentials are supplied via CI secrets / environment, not hardcoded in source, so switching accounts changes no application code.
- R5. While on the individual Team ID, adopt no team-scoped Apple capabilities (e.g. CloudKit, push, app groups, Sign in with Apple) that would bind to the account and complicate migration.
- R6. The org switchover is delivered as a single, deliberate "re-grant" release: testers are warned in advance and, once, re-grant the three TCC permissions **and** re-approve the Screencap helper in Login Items. The three heavy grants (Screen Recording, Accessibility, Input Monitoring) are owned by the **daemon helper** (`com.screencap.daemon`), not the app — re-signing the helper with the org Team ID orphans them, and because the helper is registered via SMAppService, the Team-ID change also triggers a one-time Login-Items re-approval. Still one release; two clicks instead of one.

---

## Acceptance Examples

- AE1. **Covers R1.** Given a tester on a clean Mac, when they download and open a current (individual-signed, notarized) build, then it launches without a Gatekeeper block and the permissions walkthrough guides first-time grants.
- AE2. **Covers R4, R6.** Given the org account is live, when a release is cut under the org identity, then the only changes are configuration/secrets (no bundle-id or source edits) and testers receive a Slack heads-up before updating.
- AE3. **Covers R6.** Given a tester updates to the first org-signed build, when they launch it, then the three daemon-owned permissions (Screen Recording / Accessibility / Input Monitoring) show as not-granted once and the helper needs Login-Items re-approval, and both are restored through a single walkthrough + Quit & Relaunch pass. Stale individual-signed Privacy entries may remain visible and are expected.

---

## Success Criteria

- Testers can install and run notarized builds now, during the org-enrollment window, without permission or Gatekeeper friction on first install.
- The eventual org switch costs one Slack heads-up plus one tester re-grant — with no bundle-id change, no app transfer, and no application-code edits.
- A downstream implementer can stand up the notarization/release path knowing the account is swappable by configuration alone.

---

## Scope Boundaries

- Mac App Store distribution — excluded now and as a near-term goal under the individual account; if pursued later, start fresh under the org.
- Building the notarization/release CI pipeline itself — this doc fixes the account decision and its guardrails; the pipeline mechanics are planning/implementation work.
- Auto-update (e.g. Sparkle) — out of scope for this decision; noted as possible future work.
- The CLI release pipeline — already direct via GCS/GitHub and unaffected by the app's signing identity.
- Growing the tester cohort to non-technical / non-reachable users before the org flip — would raise the re-grant cost and is deliberately not part of this window.

---

## Key Decisions

- Ship under the individual membership now rather than wait for the org account: ~2 weeks of early tester feedback outweighs sparing a handful of friendly testers a one-time re-grant.
- Direct/notarized distribution, not App Store: makes the org switch a re-sign rather than an app-transfer, and lets the bundle id stay constant.
- Account identity lives in CI/secrets, extending the existing `DEVELOPMENT_TEAM` env pattern to release: the org switchover touches zero source.
- Stay capability-minimal on the individual Team ID: no team-scoped Apple services adopted while on the temporary identity.

---

## Dependencies / Assumptions

- Org enrollment completes in roughly the expected window (~2 weeks); if it slips materially, revisit whether to keep growing the tester cohort under the individual identity.
- Tester cohort stays small and Slack-reachable through the org flip, keeping the one-time re-grant a non-event.
- No team-scoped Apple capabilities are in use today (verified: entitlements declare only audio-input; no CloudKit/push/app-groups), so the migration cost is re-sign + one daemon-grant re-grant + one helper re-approval.
- The individual Apple Developer Program membership is technically sufficient for the full architecture — one Developer ID Application cert signs both the app (`com.screencap.macos`) and the bundled daemon helper (`com.screencap.daemon`), and SMAppService registration of a per-user LaunchAgent needs no special entitlement or provisioning profile. Nothing here is gated to an organization account.
- License-clean boundary to hold: the daemon stays a plain LaunchAgent. Adopting a **System Extension** or **Endpoint Security client** (`com.apple.developer.endpoint-security.client`) — the one daemon shape that requires a team-scoped, Apple-granted entitlement + provisioning profile — would entangle the individual→org migration and is out of bounds while on the individual account.

---

## Outstanding Questions

### Deferred to Planning

- [Affects R1, R4][Technical] Exact signing/notarization mechanism (Developer ID Application certificate, notary credential type, and where each is stored in CI) — resolve during planning of the release path.
- [Affects R6][Technical] Whether the existing permissions walkthrough needs any copy/UX tweak to frame the one-time post-flip re-grant clearly, or works as-is.
