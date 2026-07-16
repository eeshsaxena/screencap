# Manual QA — intelligence-setup-honest-status

The feature is fully implemented and machine-verified (daemon: pytest; Swift: compile
+ `xcodebuild test` from `~/dev`, no regressions). The items below are the behavioral
checks a compile + unit test can't cover — run them on a real Mac before merge.

## First-recording beat (U7)
- [ ] On a fresh install with the onboarding choice **not** made, starting the first
      recording presents the beat exactly once; a second recording does **not**.
- [ ] Beat content adapts: a usable model → light "Intelligence is ready" confirm; no
      usable model → download / connect / "Not now"; while the verdict is still loading
      → the waiting state (never choose-a-model flashed at a usable-model user).
- [ ] A user who completed the download-model step in onboarding is **not** re-asked.
- [ ] The beat never blocks or delays the recording (it starts underneath).
- [ ] "Download on-device model" starts the download in place; "Connect your own model"
      opens the Intelligence pane; "Not now" dismisses.

## Dead-state banner (U8)
- [ ] On a *later* recording with no usable model, the New Recording sheet shows the
      banner; it **never blocks the Record button**.
- [ ] On the very first recording the banner is suppressed (the beat owns that ask).
- [ ] "Set up" opens the Intelligence pane; the [x] dismisses it for that recording.
- [ ] When Apple Intelligence is genuinely usable, neither the banner nor the sidebar
      "Name your tasks" hint appears (probe-aware gate).

## Honest empty-card (U6)
- [ ] A recording with no AI tasks shows the correct distinct copy per state:
      not-set-up / couldn't-run / still-processing / nothing-to-name / unknown.
- [ ] A degraded (mechanical-names) recording shows the "mechanical names" banner
      **above** its populated task list, visibly distinct from AI-named tasks.

## Honest "Ready" (U6/settings)
- [ ] The Intelligence pane's readiness reflects the composed verdict, and a recording
      that ends up mechanical/couldn't-run is honestly labeled on its card.

## Notes
- Two deferred daemon items remain in `review-followups.md` (outcome↔row atomicity
  crash-window; a dedicated `intelligence.status` API-version constant).
