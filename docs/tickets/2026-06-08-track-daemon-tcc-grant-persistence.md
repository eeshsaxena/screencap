---
title: "Track: daemon TCC grant persistence across rebuilds (ad-hoc treadmill)"
status: open
priority: low
created: 2026-06-08
related_plans:
  - docs/plans/2026-06-03-001-feat-macos-app-developer-id-notarized-distribution-plan.md
  - docs/plans/2026-06-05-003-feat-daemon-tcc-permission-visibility-onboarding-plan.md
related_pr: https://github.com/proteus-computer-use/screencap/pull/223
---

# Track: daemon TCC grant persistence across rebuilds (ad-hoc treadmill)

## Problem

The daemon (`com.screencap.daemon`, the bare `screencap` binary) is the TCC
subject for capture. Under **ad-hoc** signing, its cdhash changes on every
rebuild, so a previously granted Screen Recording / Accessibility / Input
Monitoring entry is **orphaned** on the next dev build (and duplicate `screencap`
rows accumulate in System Settings). This is the documented "TCC treadmill":
Settings shows "ScreenCap allowed" but the *running* binary's identity no longer
matches the granted entry, so capture fails with a permission error.

The daemon-TCC-visibility feature (PR #223, plan
`docs/plans/2026-06-05-003-…`) makes orphaned state **recoverable in-product**
(detect → surface → re-register via the Grant verb) but **does not stop the
orphaning**.

## Ownership / why this is just a tracking ticket

Grant *persistence* is explicitly **out of scope** for the visibility feature and
is **owned by the Developer-ID / notarized-distribution plan**
(`docs/plans/2026-06-03-001-feat-macos-app-developer-id-notarized-distribution-plan.md`).
A Developer-ID-signed daemon has a stable identity (TCC keys on bundle-id +
team-id), so grants survive rebuilds. Recent work already team-signs the embedded
daemon binary (`DEVELOPMENT_TEAM` set → stable identity across rebuilds), which
mitigates the treadmill for developers who set it; full persistence lands with the
Developer-ID/notarization work.

This ticket exists only so the persistence dependency is not lost between the two
plans.

## Acceptance

- When the Developer-ID/notarization plan lands: a dev/distribution rebuild keeps
  existing daemon TCC grants (no re-grant, no orphaned/duplicate `screencap` rows).
- The visibility feature's in-product recovery path remains the fallback for any
  residual orphaning.

## Notes

- Related dev/QA papercut from the same area:
  `docs/tickets/2026-06-08-fix-stale-daemon-squats-socket.md`.
- Sequoia monthly re-auth / `com.apple.developer.persistent-content-capture` is a
  separate, deferred concern (Apple-form entitlement, not self-service).
