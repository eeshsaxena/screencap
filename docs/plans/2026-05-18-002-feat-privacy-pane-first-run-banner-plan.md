---
title: "feat: SwiftUI v1 Phase 4 — Privacy pane + first-run banner"
type: feat
status: completed
date: 2026-05-18
origin: https://linear.app/zk-email/issue/SCR-17/swiftui-v1-phase-4-privacy-pane-first-run-banner
---

# feat: SwiftUI v1 Phase 4 — Privacy pane + first-run banner

## Summary

Ship the v1 privacy + onboarding polish for the macOS SwiftUI shell. Adds a per-app Privacy pane that lists installed apps with state badges derived from `screencap apps --json` and round-trips toggles through `screencap settings privacy` (no Swift-side TOML writes), plus a non-blocking first-run banner with a menu bar attention dot. Copy is honest about matrix behavior — browser content and AI tools remain visible in playback. Adds a small read-side extension to `screencap settings --json` so SwiftUI can determine banner state without bypassing the CLI as the single source of truth.

---

## Problem Frame

SCR-13 (SwiftUI v1 umbrella) needs onboarding + per-app privacy controls before the friend-trial DMG ships. Today the Privacy sidebar section renders a "Coming in Unit 18" stub ([macos/ScreenCap/Views/MainWindow.swift:213](macos/ScreenCap/Views/MainWindow.swift:213)), and there is no in-app disclosure of what the privacy matrix actually does — non-technical users get a working recording with default privacy behavior but no visibility into which apps are blocked, masked, or text-redacted. The CLI surface for both reads (`screencap apps --json`) and writes (`screencap settings privacy <field> <op> <value>`) already exists from Phase 1; this plan is the SwiftUI consumer side plus one small CLI read-side gap.

---

## Requirements

- R1. The Privacy sidebar section renders a scrollable list of `~50+` installed apps with per-row state badges that match the resolved privacy action under the configured mode.
- R2. The toggle on each row round-trips through `screencap settings privacy exclude_apps add|remove <bundle_id>` — no Swift-side writes to `~/.screencap/config.toml`.
- R3. Matrix-EXCLUDE rows (where `is_matrix_exclude == true`, in practice the `PASSWORD_MANAGER` class — see Inferred bets) show a disabled toggle and the "Always blocked (security)" badge.
- R4. The configured `mode` is shown as read-only text in the pane header (v1 — full mode picker deferred).
- R5. A non-blocking first-run banner renders at the top of the main window detail area with the exact honest copy from the ticket, including the explicit "Browser content and AI tools (ChatGPT, Claude) are visible in playback" disclosure.
- R6. The banner has two CTAs: "Review what's captured" (navigates to the Privacy sidebar section) and "I'll configure later" (writes `setup_skipped=true`). Both CTAs clear the banner.
- R7. A menu bar attention dot accompanies the banner — visually distinct from the existing red recording dot — and clears in lockstep with the banner.
- R8. On first SwiftUI launch with no `[privacy]` section, the app writes `mode = internal` via `screencap settings privacy mode set internal` (default fail-closed).
- R9. R16 invariant preserved: mode is never accidentally overwritten by exclude/allow toggles — all writes flow through the existing CLI helpers that already centralize this.
- R10. `screencap settings --json` exposes the `[privacy]` scalar fields (`mode`, `setup_skipped`) and a `has_privacy_section` derived flag so the SwiftUI banner state machine has a single readback path.

---

## Scope Boundaries

- Full mode picker (PUBLIC / INTERNAL switcher) in the Privacy pane — read-only text in v1.
- In-app classification controls for `app_classes`, `mask_domains`, `mask_title_patterns` — sub-line in the banner points users to `screencap setup` in Terminal (R17v1 honest fallback; native bridge is v1.2 per SCR-13).
- NLP model download UX (deferred to v1.2 per parent umbrella).
- Live filesystem watcher on `~/.screencap/config.toml` — pane refreshes on appear + manual refresh button only in v1.
- Toolbar refresh button on the Privacy pane (the existing `RecordingsListView` refresh in the toolbar refreshes recordings, not apps — separate concern).
- Search / filter input over the app list — punted to follow-up if friend-trial feedback shows it's needed. `~50+` apps is a single-screen scroll on most displays.

---

## Context & Research

### Relevant Code and Patterns

- [macos/ScreenCap/Views/MainWindow.swift:213](macos/ScreenCap/Views/MainWindow.swift:213) — sidebar Privacy section currently disabled. The detail area is where `PrivacyPaneView` will be wired in.
- [macos/ScreenCap/Controllers/PermissionController.swift](macos/ScreenCap/Controllers/PermissionController.swift) — `@MainActor ObservableObject` pattern that `PrivacyController` should mirror (async refresh, `@Published` state, bound into the scene via `@StateObject` in `ScreenCapApp`).
- [macos/ScreenCap/Controllers/CLIClient.swift](macos/ScreenCap/Controllers/CLIClient.swift) — `runJSON` / `runAwaitingExit` are the existing entry points. New CLI invocations should call these rather than re-spawning `Process` directly. Note the `--json` assertion at [CLIClient.swift:124](macos/ScreenCap/Controllers/CLIClient.swift:124).
- [macos/ScreenCap/Views/RecordingBanner.swift](macos/ScreenCap/Views/RecordingBanner.swift) — closest visual precedent for a non-blocking banner above the detail area; reuse its overlay/transition idiom for the first-run banner.
- [macos/ScreenCap/ScreenCapApp.swift:47](macos/ScreenCap/ScreenCapApp.swift:47) — `MenuBarExtra` label site where the attention dot will be composed.
- [src/screencap/cli/__init__.py:1839](src/screencap/cli/__init__.py:1839) — `apps` command, schema v2; returns the exact fields needed (`resolved_action`, `in_exclude_apps`, `in_allow_apps`, `is_matrix_exclude`, `has_per_frame_overrides`, `classification_source`).
- [src/screencap/cli/__init__.py:3333](src/screencap/cli/__init__.py:3333) — `settings privacy` write command with symmetric JSON envelope (`_SETTINGS_PRIVACY_SCHEMA_VERSION`). Already validates `allow_apps add` against the matrix action under the configured mode.
- [src/screencap/cli/__init__.py:3131](src/screencap/cli/__init__.py:3131) — `settings` group + `settings --json` (the read-side that U1 extends).
- [src/screencap/privacy/policy.py:69](src/screencap/privacy/policy.py:69) — the `_ACTION_MATRIX`. Confirms that under any mode, only the `PASSWORD_MANAGER` class is EXCLUDE for all modes (BANKING/AUTH/PAYMENT relax to `MASK_WINDOW` under `INTERNAL`).

### Institutional Learnings

- [docs/solutions/integration-issues/macos-foundation-process-pipe-pitfalls.md](docs/solutions/integration-issues/macos-foundation-process-pipe-pitfalls.md) — Foundation.Process + Pipe pitfalls (stdout deadlock, termination-handler race, TCC subject identity, timer-driven spawn fork-bombs). `CLIClient` already addresses these; this plan should not bypass `CLIClient` for its new invocations.
- [docs/solutions/ui-bugs/swiftui-windowgroup-vs-window-singleton-scene-2026-05-18.md](docs/solutions/ui-bugs/swiftui-windowgroup-vs-window-singleton-scene-2026-05-18.md) — `MainWindowID` is a singleton `Window`, not a `WindowGroup`. Banner state lives on a long-lived `PrivacyController` (singleton via `@StateObject` in `ScreenCapApp`) so it survives the menu bar / Dock reopen flow.

### External References

- None — feature is wired entirely against the existing in-repo CLI surface and SwiftUI primitives.

---

## Key Technical Decisions

- **All Python config writes go through the existing `screencap settings privacy <field> <op> <value>` helper, never via Swift TOML serialization.** Rationale: preserves the R16 invariant (mode never silently mutated by exclude/allow writes), keeps the advisory flock + symmetric-envelope semantics in one place, and means the Privacy pane behaves identically whether toggled from SwiftUI or `screencap` in Terminal.
- **Banner state derives from `setup_skipped`, read via an extended `screencap settings --json` payload.** Rationale: matches the user's selected option; gives SwiftUI a single round-trip readback path that respects the CLI as source of truth, and avoids a SwiftUI-side `UserDefaults` flag that would drift if the user mutates state via `screencap` in Terminal.
- **"Review what's captured" CTA marks setup complete on pane visit alone** (writes `setup_skipped=true` the first time `PrivacyPaneView.onAppear` fires while the banner is active). Rationale: matches the user's selected option; simplest state machine and lowest friction. Trade-off acknowledged: a user could navigate away without scanning the list. Mitigation: the banner copy itself is the disclosure, the pane is the secondary affordance.
- **`PrivacyController` is a singleton `@StateObject` in `ScreenCapApp`**, alongside `recorder` / `permissions` / `index`. Banner state is read from it both inside `MainWindow` and from the `MenuBarExtra` label in the same scene, so a single source of truth drives both surfaces.
- **App icon loading happens Swift-side via `NSWorkspace.shared.icon(forFile: path)`.** Rationale: `apps --json` returns `icon_path = ""` by design (see comment at [cli/__init__.py:1944](src/screencap/cli/__init__.py:1944)). Avoids shipping icon bytes through the JSON envelope.
- **Pane refreshes on appear + manual refresh button only** — no live watcher in v1. Rationale: matches `RecordingsIndex` refresh pattern; live watcher is a separate complexity that friend-trial can pull in if needed.
- **Menu bar attention dot uses SF Symbol composition** (`record.circle` base + a small offset overlay badge). Rationale: stays inside the SwiftUI `MenuBarExtra` label contract; avoids touching `NSStatusItem` directly. Visual distinctness from the red recording dot is enforced by color (accent / orange) and position (corner badge vs full-fill).

---

## Open Questions

### Resolved During Planning

- *Banner dismiss semantics for "Review what's captured"*: pane visit alone clears the banner (user-confirmed).
- *Read-side for banner state*: extend `screencap settings --json` with a `privacy` block; no new CLI verb (user-confirmed).
- *Behavior for existing CLI users with `[privacy]` section but unset `setup_skipped`*: banner appears once; the honest-copy disclosure is the value-add and the dismiss CTA is one click. Acceptable for v1 friend-trial scope.

### Deferred to Implementation

- *Exact SF Symbol for the menu bar attention dot* — depends on visual contrast against the existing `record.circle` shape; pick during U5 implementation by trying 2–3 candidates (e.g. `circle.fill` offset, `exclamationmark.circle.fill`, `circle.badge.exclamationmark`).
- *Whether the banner re-appears on app version upgrade* — leave as v1 behavior (no re-prompt); revisit if the matrix changes again.
- *Should `has_per_frame_overrides == true` decorate every "Captured" row, or only `allow`-resolved rows?* — pick during U3 based on how visually noisy the asterisk feels with real app counts.

---

## Implementation Units

### U1. Extend `screencap settings --json` with privacy readback

**Goal:** Add a `privacy` block to the existing `settings --json` payload so SwiftUI can read `mode`, `setup_skipped`, and `has_privacy_section` in one round-trip without touching `~/.screencap/config.toml` directly.

**Requirements:** R10

**Dependencies:** None.

**Files:**
- Modify: `src/screencap/cli/__init__.py` (the `settings` command at `:3138`; bump `_SETTINGS_SCHEMA_VERSION` at `:48`-ish)
- Test: `tests/test_cli.py` (or a focused `tests/test_settings_json.py` if `test_cli.py` is large — check existing convention)

**Approach:**
- Read the raw `[privacy]` section (not the parsed `PrivacyConfig`, which fills defaults) to distinguish "absent" from "present with default value" — `has_privacy_section` must be `false` when the user has never written to it (drives the U5 first-launch fail-closed write).
- Extend `settings_payload` (`:3236`-ish) with a nested `privacy: {mode, setup_skipped, has_privacy_section}` block. `mode` falls back to `"internal"` when absent (matches the PrivacyConfig default); `setup_skipped` falls back to `false`.
- Bump `_SETTINGS_SCHEMA_VERSION` — note that the existing field set on the prose path stays unchanged (prose readers don't need this), but JSON consumers do.

**Patterns to follow:**
- The existing `settings_payload` construction at [src/screencap/cli/__init__.py:3236](src/screencap/cli/__init__.py:3236) — same shape (single dict, fed into the `--json` branch).
- The "load raw TOML once" pattern at [src/screencap/cli/__init__.py:282](src/screencap/cli/__init__.py:282) (`_load_toml()` + `cfg.get("privacy", {})`).

**Test scenarios:**
- Happy path: `[privacy]` absent → response includes `"privacy": {"mode": "internal", "setup_skipped": false, "has_privacy_section": false}`.
- Happy path: `[privacy] mode = "public"` only → `mode == "public"`, `setup_skipped == false`, `has_privacy_section == true`.
- Happy path: `[privacy] setup_skipped = true` only → `setup_skipped == true`, `has_privacy_section == true`.
- Edge case: `[privacy]` present but empty table → `has_privacy_section == true`, scalars at defaults.
- Edge case: invalid `mode` value in config → falls back to `"internal"` (mirrors `PrivacyConfig` permissiveness) and does not raise.
- Schema bump: response carries the new `_SETTINGS_SCHEMA_VERSION` value; consumers detecting the old version still parse the rest of the payload.

**Verification:**
- `pytest tests/test_cli.py -k settings` (or the focused file) passes.
- `screencap settings --json` invoked with each fixture config returns the expected shape.
- `ruff check src/screencap/engine/` still passes (lint scope per CLAUDE.md is engine-only, but no regressions in the touched file).

---

### U2. `PrivacyController` + models + CLI plumbing

**Goal:** A `@MainActor ObservableObject` that owns the privacy state surface: app list with badge-derivation inputs, configured mode (read-only), `setup_skipped` / `has_privacy_section` for banner state, and write paths for toggling exclude / completing first-run setup.

**Requirements:** R2, R3, R8, R9, R10

**Dependencies:** U1 (the controller's readback consumes the extended `settings --json` payload).

**Files:**
- Create: `macos/ScreenCap/Models/InstalledApp.swift` (Codable struct matching the `apps --json` row schema v2)
- Create: `macos/ScreenCap/Models/PrivacyStatus.swift` (Codable struct matching the new `settings --json` privacy block)
- Create: `macos/ScreenCap/Controllers/PrivacyController.swift`
- Modify: `macos/ScreenCap/ScreenCapApp.swift` (add `@StateObject private var privacy = PrivacyController()` and pass it through `.environmentObject(privacy)`; bind into the `MenuBarExtra` label)
- Test: `macos/ScreenCapTests/PrivacyControllerTests.swift`

**Approach:**
- Mirror `PermissionController`: `@Published` state for `apps: [InstalledApp]`, `status: PrivacyStatus?`, `isLoading`, `lastError`. `async` methods for `refreshApps()`, `refreshStatus()`, `toggleExclude(bundleId:excluded:)`, `markSetupComplete()`, `ensureFirstLaunchModeWritten()`.
- Decode the `apps --json` envelope (`{ok, schema_version, apps: [...]}`) and surface schema-mismatch as a warning log, not a hard error — older CLIs returning v1 still partially parse.
- `ensureFirstLaunchModeWritten()` is the U5 first-run hook called once at app launch: if `status.hasPrivacySection == false`, invoke `screencap settings privacy mode set internal`, then re-fetch status.
- Wrap CLI invocations in `CLIClient.runJSON` / `runAwaitingExit` only — no bare `Process()`.

**Patterns to follow:**
- `PermissionController` (single-file `@MainActor` ObservableObject with `@Published` state, `func refresh()` async).
- `RecordingsIndex` for the loading / error state shape.
- `CLIClient.runJSON` invocation pattern (caller passes `--json` explicitly).

**Test scenarios:**
- Happy path: `refreshApps` decodes a fixture matching schema v2 → `apps` populated, no error.
- Happy path: `refreshStatus` decodes the U1 fixture → `status` populated with `mode`, `setupSkipped`, `hasPrivacySection`.
- Happy path: `toggleExclude(bundleId: "com.foo", excluded: true)` invokes the CLI with arguments `["settings", "privacy", "exclude_apps", "add", "com.foo", "--json"]`.
- Happy path: `toggleExclude(bundleId: "com.foo", excluded: false)` invokes the CLI with `remove`.
- Happy path: `markSetupComplete()` invokes the CLI with `["settings", "privacy", "setup_skipped", "set", "true", "--json"]`.
- Happy path: `ensureFirstLaunchModeWritten()` with `hasPrivacySection == false` invokes `["settings", "privacy", "mode", "set", "internal", "--json"]` and re-fetches status.
- Edge case: `ensureFirstLaunchModeWritten()` with `hasPrivacySection == true` is a no-op (no CLI call).
- Error path: `refreshApps` with a non-zero CLI exit surfaces `lastError` and leaves `apps` unchanged from prior state.
- Error path: `refreshApps` with malformed JSON (truncated stream) surfaces `lastError` without crashing the decoder.
- Idempotence: two concurrent `toggleExclude` calls for the same bundle don't both fire CLI writes (debounced / serialized by the controller).
- Integration: covers F1 (first-run mode write) — first-launch sequencing ordering of `ensureFirstLaunchModeWritten` → `refreshStatus` → banner state derivation.

**Verification:**
- `xcodebuild test -scheme ScreenCap -only-testing:ScreenCapTests/PrivacyControllerTests` passes.
- Privacy controller compiles and binds cleanly into `ScreenCapApp` without breaking other `@StateObject` initialization.

---

### U3. `PrivacyPaneView` + `PrivacyAppRow` + badge derivation

**Goal:** The SwiftUI pane itself — header with read-only mode, refresh button, scrollable list of `PrivacyAppRow`s with the badge logic, per-row toggle gated by `is_matrix_exclude`.

**Requirements:** R1, R3, R4

**Dependencies:** U2 (the pane reads from `PrivacyController`).

**Files:**
- Create: `macos/ScreenCap/Views/Privacy/PrivacyPaneView.swift`
- Create: `macos/ScreenCap/Views/Privacy/PrivacyAppRow.swift`
- Create: `macos/ScreenCap/Views/Privacy/PrivacyBadgeStyle.swift` (pure-function badge string + color derivation, separated so it can be tested without rendering)
- Test: `macos/ScreenCapTests/PrivacyBadgeStyleTests.swift`

**Approach:**
- Pane layout: header `VStack` with "Privacy" title + "Mode: internal (read-only)" subheading + manual refresh `Button`. Body is a `List` (native macOS row separators + scroll) bound to `controller.apps`.
- `PrivacyAppRow` takes an `InstalledApp` + `bannerActive: Bool` (for the toggle disabled-tooltip wording) and renders icon (via `NSWorkspace.shared.icon`), display name, bundle id (secondary), badge label, and the toggle.
- Badge derivation in `PrivacyBadgeStyle.badge(for:)` returns a `(text, color, toggleDisabled, tooltip)` tuple from the row's fields. This is the unit under test for U3.
- Per-frame overrides: `has_per_frame_overrides == true` decorates `allow`-resolved rows with a trailing `*` and a tooltip explaining that domain/title overrides may still mask at capture time.
- Toggle action: `Task { await controller.toggleExclude(bundleId:..., excluded: newValue) }`, then optimistic update of the local row state (re-derived on next `refreshApps`).

**Patterns to follow:**
- [macos/ScreenCap/Views/RecordingsListView.swift](macos/ScreenCap/Views/RecordingsListView.swift) — `List` + per-row view with secondary text.
- [macos/ScreenCap/Views/PrivacyMatrixDisclosureView.swift](macos/ScreenCap/Views/PrivacyMatrixDisclosureView.swift) — label / icon idiom for privacy-themed UI.

**Test scenarios:**
- Covers R3. `is_matrix_exclude == true` → badge text `"Always blocked (security)"`, toggle disabled.
- `in_exclude_apps == true` (and not matrix-exclude) → `"Excluded by you"`, toggle enabled and ON.
- `in_allow_apps == true` and `resolved_action != exclude` → `"Captured (allowed by you)"`, toggle enabled and OFF.
- `resolved_action == mask_window` (no overrides) → `"Captured (window masked)"`, toggle enabled and OFF.
- `resolved_action == text_redact` → `"Captured (text redacted)"`, toggle enabled and OFF.
- `resolved_action == allow` → `"Captured"`, toggle enabled and OFF.
- Edge case: `has_per_frame_overrides == true` + `resolved_action == allow` → badge text decorated with `*`, tooltip non-empty.
- Edge case: `is_matrix_exclude == true` + `in_exclude_apps == true` → matrix-exclude wins; badge stays `"Always blocked (security)"`, toggle disabled (user override is redundant, not visible).
- Edge case: empty app list → pane shows an empty state, not a frozen ProgressView.

**Verification:**
- `xcodebuild test -only-testing:ScreenCapTests/PrivacyBadgeStyleTests` passes.
- SwiftUI Preview renders the pane with a fixture of 5–8 apps covering each badge state.

---

### U4. Enable Privacy sidebar section in `MainWindow`

**Goal:** Wire `PrivacyPaneView` into the existing sidebar slot — remove the `.disabled(true)` and "Coming in Unit 18" stub. This unit also wires the "Review what's captured" CTA target (U5 navigates to `section = .privacy`).

**Requirements:** R1, R6

**Dependencies:** U3 (the pane exists), U2 (controller is in the environment).

**Files:**
- Modify: `macos/ScreenCap/Views/MainWindow.swift` (remove `.disabled(true)` at `:125`; swap the stub `VStack` at `:213` for `PrivacyPaneView()`)

**Approach:**
- Pull `@EnvironmentObject private var privacy: PrivacyController` into `MainWindow`.
- In `sectionContent` at `:197`, replace the `.privacy` case body with `PrivacyPaneView()`.
- Remove `.disabled(true)` from the Privacy `NavigationLink`.
- Behavior change is intentionally narrow: nothing in the existing `.calendar` / `.recordings` flow moves.

**Patterns to follow:**
- Existing `.calendar` / `.recordings` cases in the same `sectionContent` switch.

**Test scenarios:**
- Test expectation: none beyond a compile/preview check — this is a wiring unit. The behavior under test belongs in U3 (pane content) and U5 (navigation from banner CTA). If the pane fails to render here, U3 tests would have caught the rendering issue first.

**Verification:**
- App launches; clicking "Privacy" in the sidebar shows the pane (manual SwiftUI Preview + run in dev).
- No regressions in `RecorderControllerTests` / `WindowOpenerTests` (sanity — they don't touch this view but the scene wiring should not break).

---

### U5. First-run banner + first-launch mode write + menu bar attention dot

**Goal:** The non-blocking banner, the first-launch fail-closed `mode = internal` write, the menu bar attention dot that follows banner state, and the two CTA handlers.

**Requirements:** R5, R6, R7, R8

**Dependencies:** U2 (`PrivacyController` exposes banner state + write paths), U4 ("Review what's captured" needs the Privacy section enabled).

**Files:**
- Create: `macos/ScreenCap/Views/Privacy/FirstRunPrivacyBanner.swift`
- Modify: `macos/ScreenCap/Views/MainWindow.swift` (mount banner above the existing `RecordingBanner` in the detail `VStack` at `:34`; wire the "Review what's captured" CTA to set `section = .privacy` and call `privacy.markSetupComplete()`)
- Modify: `macos/ScreenCap/ScreenCapApp.swift` (compose the menu bar attention dot in the `MenuBarExtra` label at `:47`; call `privacy.ensureFirstLaunchModeWritten()` + `privacy.refreshStatus()` in the existing `.task` block at `:36`)
- Modify: `macos/ScreenCap/Views/Privacy/PrivacyPaneView.swift` (on `.onAppear`, if banner is active, call `privacy.markSetupComplete()` — this is the "pane visit alone clears banner" semantic)
- Test: `macos/ScreenCapTests/FirstRunPrivacyBannerTests.swift` (banner state machine + first-launch write sequencing)

**Approach:**
- `FirstRunPrivacyBanner` is a stateless `View` that renders the honest copy + two CTAs + a small `[x]` (so the dismiss affordance isn't only the two buttons), and takes two closures (`onReview`, `onDismiss`). Visual style: muted background (matches `.controlBackgroundColor` per `FirstRunPermissionsView` precedent), `lock.shield` icon, body text + sub-line.
- `MainWindow` derives `bannerActive` from `privacy.status?.setupSkipped == false`. When `bannerActive` is true, render the banner above `RecordingBanner` in the existing detail `VStack` (above the recording banner so an active recording doesn't push the first-run banner below the fold).
- `MenuBarExtra` label composition: `ZStack` of `Image(systemName: recorder.state.isRecording ? "record.circle.fill" : "record.circle")` plus, when `privacy.bannerActive`, a small badge overlay (initial pick: `Image(systemName: "circle.fill").font(.system(size: 5)).foregroundStyle(.orange)` offset to upper-right). Final SF symbol chosen during implementation.
- First-launch sequencing in `ScreenCapApp.task`: `ensureFirstLaunchModeWritten()` (no-op if `[privacy]` already exists) → `refreshStatus()` → `refreshApps()`. Runs after the existing `recorder.probeDaemon()` to avoid contending with the daemon probe.
- The "Review" CTA flow: closure body sets `section = .privacy`, then awaits `privacy.markSetupComplete()`. The pane's `.onAppear` is an *additional* dismiss hook for the case where the user navigates to the pane via the sidebar directly (not via the CTA) — both paths go through `markSetupComplete()`, which is idempotent.
- The "I'll configure later" CTA flow: awaits `privacy.markSetupComplete()`. No navigation.
- The `[x]` close affordance: same behavior as "I'll configure later" — both invoke `markSetupComplete()` (lower-friction dismiss for users who don't want to pick between the two CTAs).

**Patterns to follow:**
- `RecordingBanner` for the banner shape + overlay idiom.
- `FirstRunPermissionsView` for the in-card layout / `.controlBackgroundColor` styling.

**Test scenarios:**
- Covers R8. First launch with `hasPrivacySection == false`: `ensureFirstLaunchModeWritten` invokes the `mode set internal` CLI write exactly once; subsequent launches (with `hasPrivacySection == true`) skip it.
- Covers R5 + R6. Banner state machine: `status == nil` → banner hidden (still loading). `status.setupSkipped == false` → banner shown. `status.setupSkipped == true` → banner hidden.
- Covers R6. "I'll configure later" tap → `markSetupComplete()` invoked, banner clears on next `status` update.
- Covers R6. "Review what's captured" tap → `section` becomes `.privacy` AND `markSetupComplete()` invoked.
- Covers R6. Pane visit via sidebar (not via CTA) while banner is active → `markSetupComplete()` invoked in `.onAppear`.
- Covers R7. Menu bar attention dot visible iff `bannerActive == true`; coexists with the red recording dot when the user is recording.
- Edge case: rapid double-tap on "I'll configure later" fires `markSetupComplete()` only once (controller serializes).
- Edge case: U1's `settings --json` returns malformed payload → `status` stays nil, banner stays hidden (fail-closed for UI, not for security).
- Integration: full first-launch flow — app launches, daemon probe completes, `ensureFirstLaunchModeWritten` fires, `refreshStatus` returns `setupSkipped=false`, banner appears, user clicks Review, `section` flips to `.privacy`, pane renders, `markSetupComplete` fires, banner clears on next render tick. (Drives the first-friend-trial Done-when criterion.)

**Verification:**
- `xcodebuild test -only-testing:ScreenCapTests/FirstRunPrivacyBannerTests` passes.
- Manual SwiftUI run on a fresh `~/.screencap/` directory: banner appears, dot appears, clicking each CTA clears both.
- Manual run on a config with `setup_skipped = true`: no banner, no dot.

---

## System-Wide Impact

- **Interaction graph:** `PrivacyController` is read by `MainWindow` (banner gate + pane content), `ScreenCapApp` (menu bar label dot), and `PrivacyPaneView` (`.onAppear` dismiss hook). All three depend on `@Published` state from the same singleton instance.
- **Error propagation:** CLI failures surface in `controller.lastError` and render in the pane header (similar to `RecordingsIndex` error state) — they do NOT pop modal alerts. A failed first-launch mode write logs a warning and leaves `hasPrivacySection` as-is; the banner still shows so the user can retry the dismiss flow.
- **State lifecycle risks:** The first-launch write is gated by `hasPrivacySection == false`. Two app instances launching simultaneously (rare — singleton via `LSUIElement` + activation policy) would both try to write; the advisory flock in `_privacy_config_writer()` serializes them. Toggle writes are similarly safe.
- **API surface parity:** SwiftUI is a consumer of the existing CLI surface. The only API addition is the `privacy` block inside `settings --json` (U1) — this is a backward-compatible additive change; the schema version bump signals it to consumers that care.
- **Integration coverage:** The first-launch sequencing (U2 + U5) requires an integration-level test in `FirstRunPrivacyBannerTests` because unit-level mocks don't prove the ordering of `ensureFirstLaunchModeWritten` → `refreshStatus` → banner derivation.
- **Unchanged invariants:** R16 (mode preservation through any single privacy field write) stays in Python's `_settings_privacy_apply`. The matrix in `policy.py` is unchanged. The existing first-run permissions sheet flow (`FirstRunPermissionsView`) is unchanged — banner + permissions sheet are independent surfaces (sheet for TCC, banner for app-classification disclosure).

---

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| Existing CLI users who completed `screencap setup` (have `[privacy]` section, `setup_skipped` unset) will see the banner once on first SwiftUI launch. | Accepted for v1 — the honest-copy disclosure is the value; one-click dismiss is low friction. Revisit if friend-trial feedback flags it. |
| `screencap apps --json` for `~50+` apps via `runOneShot` could exceed the default 10s timeout on slow disks or with `--include-spotlight` (we don't pass it, but worth noting). | The pane uses the non-spotlight default. If the 10s budget proves tight, U2 can bump the timeout for this one call; the `runJSON` default is per-call configurable. |
| Menu bar dot composition could clash visually with the recording red dot when both are active. | U5 test forces the both-active case; visual tuning happens at implementation time. The dot is small + colored distinctly. If clash is severe, fall back to suppressing the attention dot while recording (acceptable trade-off — banner still shows in-window). |
| `has_per_frame_overrides == true` on every `allow` row could make the pane feel noisy. | Defer the visual treatment to U3 implementation (asterisk vs. badge vs. tooltip-only). Listed as a deferred-to-implementation question above. |
| Schema bump in `settings --json` (U1) could surprise external consumers. | Schema bump is additive; existing fields unchanged. Older consumers ignore the new `privacy` key. |

---

## Documentation / Operational Notes

- Add a brief note to `CLAUDE.md` under the existing "Key Patterns" section that all SwiftUI writes to `[privacy]` config go through `screencap settings privacy`. (Optional — the rule is enforced by code; the note helps future agents reading the repo.)
- No release/rollout/monitoring impact — feature lands inside the SwiftUI shell which is gated to the bundled DMG path.
- The `setup_skipped` flag is now read by SwiftUI in addition to its existing CLI-side write paths. If a future CLI feature mutates the flag, banner state will refresh on the next app launch (or manual refresh).

---

## Sources & References

- **Origin issue:** [SCR-17 — SwiftUI v1 Phase 4: Privacy pane + first-run banner](https://linear.app/zk-email/issue/SCR-17/swiftui-v1-phase-4-privacy-pane-first-run-banner)
- **Parent umbrella:** [SCR-13 — Native macOS SwiftUI app — v1 (umbrella)](https://linear.app/zk-email/issue/SCR-13/native-macos-swiftui-app-v1-umbrella)
- **Related institutional learnings:** [docs/solutions/integration-issues/macos-foundation-process-pipe-pitfalls.md](docs/solutions/integration-issues/macos-foundation-process-pipe-pitfalls.md), [docs/solutions/ui-bugs/swiftui-windowgroup-vs-window-singleton-scene-2026-05-18.md](docs/solutions/ui-bugs/swiftui-windowgroup-vs-window-singleton-scene-2026-05-18.md)
- **Key CLI entry points:** `src/screencap/cli/__init__.py` — `apps` (`:1839`), `settings` (`:3138`), `settings_privacy` (`:3341`)
- **Key SwiftUI entry points:** [macos/ScreenCap/ScreenCapApp.swift](macos/ScreenCap/ScreenCapApp.swift), [macos/ScreenCap/Views/MainWindow.swift](macos/ScreenCap/Views/MainWindow.swift), [macos/ScreenCap/Controllers/PermissionController.swift](macos/ScreenCap/Controllers/PermissionController.swift), [macos/ScreenCap/Controllers/CLIClient.swift](macos/ScreenCap/Controllers/CLIClient.swift)
