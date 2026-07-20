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

## Done — U6/U8 UI wiring (compiles; needs runtime QA on a signed build)
- `PresenceGatedContent(gate:)` adopted on `ScreenshotTruthPane` (optional gate) and
  the Recall palette results list (`RecallPaletteView` owns one shared `@StateObject`
  `PresenceGate`, handed to the content only when `corpus_encrypted`). `settings --json`
  + `PrivacyStatus` gained `corpus_encrypted` so the app knows when to gate.
- `SearchDisclosureView` presented by `MainWindow` as a one-time sheet after any
  onboarding/permission takeover clears, gated on `SearchDisclosurePolicy.shouldPresent`
  (covers new installs post-onboarding AND existing installs — avoids touching the
  test-pinned `OnboardingStepPolicy` derivation). Enable → `search enable`; Decline →
  `content_index_consent_declined=true`.
- **Remaining runtime QA (needs a signed build):** the visual/interaction behavior of
  the gate prompt + disclosure sheet timing is unverified until the app is signable
  (the decrypt path is dark in dev). Confirm on a signed build: gate prompts once per
  session on opening encrypted search stills; disclosure presents once, not over a
  takeover; decline holds.

## Verification Contract status
- **Python full suite green + privacy-marked + Vision-free** ✓ (the pre-existing
  `test_config`/daemon-`read_only_verbs` failures are real-`config.toml` pollution on
  this box — they fail on clean `main` too; the concurrent `fix/daemon-test-config-hermeticity`
  branch fixes exactly that harness issue).
- **Two call-graph guards pass** ✓.
- **Daemon-family failures are inter-test-class INTERFERENCE, not my code:** `DaemonClientTests` (the tests for the one daemon file I modified) passes ALONE (19 tests, 0 failures); it only fails when the daemon classes share a test process. Proven not-config (fails on clean HOME too) and not-a-regression (fails on clean `main`).
  - **Concrete diagnosis for the harness fixer** (`fix/daemon-test-config-hermeticity`): the daemon test classes set a **process-global** `SCREENCAP_DAEMON_SOCKET` in `setUp`/unset in `tearDown` (each with a unique `/tmp/sc-*.sock`). Two contributing causes: (1) **parallel test execution** races on that global env — running with `-parallel-testing-enabled NO` drops the daemon failures from ~17 → ~10; (2) the remaining ~10 are **cross-class state leaks** (a leaked/retained `DaemonClient` connection or async task from an earlier class hits a later class's server — the "Unexpected request path /v0/tasks.list" symptom). Fix path: mark the daemon test target/classes non-parallelizable in the scheme AND ensure each class fully tears down its client/tasks (or inject the socket path rather than a global env). Not attempted here — out of the search-guardrails scope, owned by that branch, and the user chose "accept code-complete".
- **Swift `xcodebuild test` green** ✓ — **940 tests, 0 failures** (19s, `test-without-building`)
  excluding the 5 pre-existing-flaky daemon-family classes the gate itself caveats
  (`DaemonClient`, `DaemonClientBackfill`, `DaemonInstallController`, `DaemonSessionService`,
  `RecorderControllerDaemon`) — they fail **identically on clean `main`** (verified in a
  control worktree) and are being fixed on a separate `fix/daemon-test-config-hermeticity`
  branch. My change contributes zero failures.
- **End-to-end** — automated in two layers:
  - Data plane (`tests/test_search_guardrails_e2e.py`): gate default-on → encrypted
    capture (only `.jpg.enc`) → secrets scrubbed at index (planted secret not findable,
    region painted) → SQLCipher search → `frame.read` serves the scrubbed still →
    key-pull → video-only.
  - **Real `screencap` CLI in an isolated HOME** (`tests/test_search_cli_e2e.py`): the
    DoD flip/decline behaviors against the actual binary — `search enable` migrates an
    existing plaintext corpus with **zero plaintext stills remaining**, rekeys the index,
    flips the gate ON (only after acknowledgment), preserves the data; declining durably
    holds the gate OFF.
  - **Remaining manual residual** (needs a signed build + human): the disclosure *sheet
    rendering*, the Touch ID *prompt* on opening encrypted search stills, and a
    new-install run — the *decisions/data* behind all three are automated above; only the
    on-screen visuals are unverified until the app is signable.

## Remaining — run the migration on the REAL dev install (mutates real data)
- `screencap search enable` executes the plaintext→encrypted migration on the user's
  actual `~/.screencap` (encrypts every existing still, rekeys `content_index.db`).
  It's headless-runnable, but it is a **hard-to-reverse mutation of real recordings**,
  so run it only with the user's explicit go-ahead. Machinery is unit + E2E tested.

## Remaining — U6 app provisioning (Swift) — the release blocker
- **EMPIRICALLY CONFIRMED:** adding `keychain-access-groups` to `Screencap.entitlements`
  **breaks the ad-hoc-signed dev/test build** — `xcodebuild` fails with *"Screencap has
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
