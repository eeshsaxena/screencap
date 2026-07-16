# Remaining Swift UI integration — intelligence-setup-honest-status

The Swift **model / policy / decode** layer is done and compile-verified (`xcodebuild
build-for-testing` in a `~/dev` worktree). What remains is the view + controller
*integration* — the runtime-behavioral parts a compile check can't validate. Finish
these on a machine that can **run** the app + the XCTest suite.

## Done + compile-verified
- U4/U5: `IntelligenceVerdict` (app-composed usable verdict) + `RecordingHonestState`
  resolver (`Models/IntelligenceVerdict.swift`) + `TasksListResponse.reason` decode.
  Unit tests: `IntelligenceVerdictTests`.
- U6: `JournalCard` renders the honest state per recording (mechanical-names banner
  above a populated list; distinct not-set-up / couldn't-run / still-processing /
  nothing-to-name / unknown empty states). Reason threaded via `JournalTasks`.
- U7/U8 policy: `Models/IntelligenceSurfacePolicy.swift` (beat catch-up gate + adaptive
  `BeatMode`; dead-state banner gate) + `IntelligenceSurfacePolicyTests`. Markers in
  `HUDHintStore` (`hasRecordedOnce`, `intelligenceChoiceSeen`).
- U8: `shouldShowLocalModelHint` is probe-aware (`verdictUsable`).

## Remaining (needs runtime verification)

### U7 — first-recording beat
- New `FirstRecordingBeatSheet` view: adaptive per `IntelligenceSurfacePolicy.beatMode`
  — `.chooseModel` reuses `OnboardingDownloadModelStep` content; `.lightConfirm` is a
  light "you're set" acknowledgement; `.awaitVerdict` waits (don't render choose-a-model
  to a usable-model user — AE2).
- Fire it from `RecorderController.start()`, **non-blocking**, gated by
  `IntelligenceSurfacePolicy.shouldShowFirstRecordingBeat(hasRecordedOnce:intelligenceChoiceSeen:)`.
  Call `HUDHintStore().markRecordedOnce()` only **after** the verdict resolves or the
  user acts (not before — AE2/KTD3).
- Write `HUDHintStore().markIntelligenceChoiceSeen()` at `OnboardingDownloadModelStep`
  on **both** a choice and a skip (this is what makes the beat catch-up).
- Setup actions act in place with inline error/retry on failure (KTD4 — no silent
  revert); `route = .intelligence` is the secondary deep-link.
- Verify: the beat fires **exactly once**, only for the catch-up cohort, adaptively,
  and never blocks `start()`.

### U8 — dead-state banner
- New non-blocking banner in `NewRecordingSheet` (reuse the `InlineStartError` pattern),
  gated by `IntelligenceSurfacePolicy.shouldShowDeadStateBanner(verdict:suppressedThisRecording:)`.
- Per-recording suppression: set when the beat was just skipped OR the banner dismissed
  this recording (R4 no-double-ask); cleared when a new recording starts.
- Verify: appears only in the dead state, **never blocks the record button**, respects
  no-double-ask + per-recording dismissal.

### Runtime verification for the whole Swift half
- Run the XCTest suite on a Mac (not in `~/Documents`): `IntelligenceVerdictTests`,
  `IntelligenceSurfacePolicyTests`, plus the U6 card behavior. These are compile-checked
  here but **not run** (launching the test host risks the TCC brick in this environment).
