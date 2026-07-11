# Search-by-default guardrails — remaining work

Tracks what's left after the Python implementation (U1–U5, U7, U8-core) + the Swift
crypto/gating core (U6 core) landed. Plan:
`docs/plans/2026-07-11-001-feat-search-guardrails-default-on-plan.md`.

## Done (Python, all tested + committed)
- U1 corpus crypto, U2 encrypted still write, U3 secrets-only index scrub, U4 SQLCipher
  index, U5 retention bound + sweep, U7 migration + read-compat, U8 gate + `frame.read`
  + flip trigger + CLI. ~85 privacy-marked tests, all green.
- U6 core (Swift): `CorpusCrypto.swift` (shared-Keychain read + CryptoKit decrypt,
  **byte-verified interoperable with the Python encryptor** by `CorpusCryptoTests`)
  and `PresenceGate.swift` (LAContext + grace window + fail-closed), `PresenceGateTests`.
  Both test classes pass under `xcodebuild test`.

## Done — U6/U8 Swift (compiles under `xcodebuild build-for-testing`; 19 tests pass)
- U6 read path: `ThumbnailLoader` decrypting decode seam (`makeDecryptingDecode`,
  `decodeDownsampled(data:)`), `RecordingFrameIndex.loadFrames` enumerates `*.jpg.enc`
  (deduped), `CorpusCrypto.readStillData` + `ScreenshotTruthPane` decrypt. Tested by
  `EncryptedFrameDecodeTests` (enc enumeration + decrypt-decode + no-key placeholder).
- U6 present-user gate: `PresenceGate` (+ tests) and the reusable
  `PresenceGatedContent` SwiftUI wrapper (the per-surface adoption primitive).
- U8 disclosure: `SearchDisclosureController` + `SearchDisclosureCopy` +
  `SearchDisclosurePolicy` + `SearchDisclosureView`; `DaemonClient.RecordingStartRequest`
  gains the `capture_images` passthrough. Tested by `OnboardingSearchDisclosureTests`
  (copy honesty + enable/decline CLI args + presentation decision).

## Remaining — needs runtime UI verification (manual QA)
- Adopt `PresenceGatedContent(gate:)` on the actual display surfaces (search-result
  still grid + `ScreenshotTruthPane` + any full-size still), with ONE shared
  `PresenceGate` owned as an `@StateObject` by the shell so the grace window is shared.
  The wrapper + gate logic are done + tested; only the view-tree threading +
  interaction verification remain.
- Present `SearchDisclosureView` in the onboarding wizard flow (new installs) and as a
  one-time post-update sheet (existing installs), driven by
  `SearchDisclosurePolicy.shouldPresent`. The model/copy/actions are done + tested;
  inserting the step into `OnboardingStepPolicy`'s derivation + the post-update
  presentation trigger remain.

## Remaining — U6 app provisioning (Swift) — the release blocker
- **EMPIRICALLY CONFIRMED:** adding `keychain-access-groups` to `ScreenCap.entitlements`
  **breaks the ad-hoc-signed dev/test build** — `xcodebuild` fails with *"ScreenCap has
  entitlements that require signing with a development certificate."* So it cannot be
  committed until the app is signed with a Developer-ID cert + a provisioning profile
  that authorizes the group; it was reverted to keep the build green. `CorpusCrypto`
  is written to read the group; enabling it is purely the signing/provisioning step.
- **App `keychain-access-groups` provisioning (SCR-242 sibling — the blocker).**
  OQ4 was resolved to option (a): the app reads the corpus key from the shared group
  `2A8S6MV8DZ.com.screencap.shared`. `keychain-access-groups` is a **restricted**
  entitlement — AMFI SIGKILLs the binary unless an embedded Developer-ID provisioning
  profile authorizes it for the app's App ID (`2A8S6MV8DZ.com.screencap.macos`), with
  `com.apple.application-identifier` + `com.apple.developer.team-identifier` +
  `Contents/embedded.provisionprofile`, wired through `sign_app.sh`. It is **stripped
  under Apple Development (dev) builds**, so `CorpusCrypto.loadKey()` returns nil in
  dev — search-result/truth-pane stills render placeholders until a signed release
  build with the app profile. Mirror `docs/runbooks/scr-242-keychain-access-group-provisioning.md`
  (which covers the daemon binary) for the app target. **This needs the actual Apple
  Developer provisioning profile + a signed-build verification — infra, not code.**
- Wire `CorpusCrypto` into the decode path: `ThumbnailLoader` (its injected
  `decode:` closure is the single choke point — decrypt `*.jpg.enc` → downsample),
  `RecordingFrameIndex` (enumerate `*.jpg.enc` alongside `*.jpg`, poster fallback
  unchanged), and the search-result / `ScreenshotTruthPane` still paths. Key absent →
  hatched placeholder, never a crash.
- Wire `PresenceGate` to gate: opening Search results with still previews, the truth
  pane, and any full-size still view. NOT gated: video playback, event timeline,
  library card *poster* thumbnails. Drive with the `ViewHostingHarness` test pattern.

## Remaining — U8 UI (Swift)
- Onboarding **disclosure step** (new installs) + a **one-time post-update
  disclosure** (existing installs never re-run onboarding), both with an inline
  decline that writes `content_index_consent_declined=true`; acknowledgment writes the
  `search_disclosure_acknowledged` marker AND triggers the flip. Copy is calibrated to
  the mechanism (encrypted; known secret formats auto-redacted; N-day retention;
  pause / per-app exclude / turn off). `OnboardingSearchDisclosureTests`.
  - The flip trigger: call `screencap search enable` via `CLIClient` (the daemon then
    runs the migration in its entitled context so the key channel is consistent), or
    add a daemon verb. The headless CLI path (`screencap search enable/status`) already
    exists and is what a signed-in daemon would run.
- `DaemonClient.swift`: optional `capture_images` passthrough for an explicit user
  override (pause/resume of stills).

## Remaining — DoD verification (needs a signed build + a dev machine)
- `xcodebuild test` green for the full macOS suite (the two new classes already pass;
  the known flaky daemon-reconnect test per repo memory is unrelated).
- Manual E2E on a dev install: fresh recording on a ready system → search finds
  on-screen text, a planted fake AWS key is *not* findable and its region is painted;
  Finder shows only `.jpg.enc`; Touch ID prompts once per session; pulling the corpus
  key → next recording is video-only with a logged reason.
- Migration executed on the dev install with zero plaintext stills remaining; a
  new-install lands with search ON + disclosure shown; an existing-install upgrade
  shows the one-time disclosure and flips only after acknowledgment.

## Notes / decisions captured in code
- OQ1 (grace window) → 15 min (`PresenceGate.defaultGraceWindow`). OQ2 (retention) →
  30 days (`config.get_screenshot_retention_days` default when encrypted).
- Backfill still indexes raw (not secrets-scrubbed) — consistency follow-up: route
  the backfill through the same `LocalScrubber` as the live index pass.
- SQLCipher binding is `pysqlcipher3` (built vs `brew install sqlcipher`); the plan's
  pinned `sqlcipher3-binary` wheel has no build for the platform. Bundling
  `libsqlcipher` into the signed app for both arches is a packaging follow-up
  (see plan Risks).
