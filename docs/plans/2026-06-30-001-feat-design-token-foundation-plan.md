---
title: "feat: Design-token foundation for the macOS app"
type: feat
status: completed
date: 2026-06-30
origin: docs/brainstorms/2026-06-30-design-token-foundation-requirements.md
---

# feat: Design-token foundation for the macOS app

## Summary

Stand up a semantic design-token foundation for the ScreenCap macOS app and apply it to the launch-visible surfaces for the 2026-07-03 launch. Colors are authored as asset-catalog color sets (named roles, light/dark/high-contrast variants resolve automatically); the signature accent ships as a global `AccentColor` plus an "Aurora" lime→aqua gradient for hero moments; spacing/radius/type live in Swift token namespaces (`SCMetrics`, `SCTypography`). Full migration of the remaining surfaces and a dark-mode craft pass stay fast-follow.

---

## Problem Frame

The app is essentially stock SwiftUI — ~164 scattered inline color literals, default system-blue accent, no type/spacing scale, `.orange` overloaded for both warnings and advisories, and no dark-mode craft (see origin: `docs/brainstorms/2026-06-30-design-token-foundation-requirements.md`). With a make-or-break Product Hunt launch on 2026-07-03 where "UX & native experience" is the load-bearing track, a stock look is a first-contact trust liability for a non-technical buyer, and every future visual change re-litigates pixel values across many call sites.

---

## Requirements

- R1. One source of truth defines tokens for color, typography, spacing, and corner radius; views consume tokens, not inline literals.
- R2. Color tokens are semantic/role-based; light, dark, and high-contrast are resolutions of the same roles.
- R3. A named type scale on the system font + Dynamic Type (no custom typeface).
- R4. Named spacing + radius ramps (no magic numbers), calibrated relaxed/structural per the "calm instrument" character constraint.
- R5. (Launch) `PrivacyBadgeStyle` consumes token roles; a generalized component vocabulary is fast-follow.
- R6. The **Aurora** signature accent (lime→aqua gradient on hero moments + derived solid spring-teal for system tint) replaces system blue.
- R7. The overloaded `.orange` retires into single-meaning roles: warm-amber recording, red reserved for error, advisories de-colored to neutral, success soft green; recording vs error distinguished by shape.
- R8. For July 3, the token system is applied to the launch-visible surfaces (menubar mark, recording banner, main-window chrome/sidebar, first-run onboarding), rendering correctly in light and dark, gated by an explicit dark-validation pass.
- R9. Full migration of remaining color references / non-visible surfaces + a dedicated dark-mode/high-contrast craft pass are fast-follow.

**Origin acceptance examples:** none defined (origin had no AE section). Verification is visual + the unit tests below.

---

## Scope Boundaries

- The four launch-visible surfaces only; all other surfaces (Search, Inspect, Privacy panes, Review/Calendar) keep their current literals through launch.
- No custom/brand typeface — system font with a named scale.
- No Liquid Glass / macOS 26 SDK adoption — independent of this foundation; must not block on it.
- Motion / recording-state animation (the "breathe not blink" idea) is a separate survivor — this plan changes the recording *color + shape*, not its motion.
- The `screencap setup` CLI-string leak in `FirstRunPrivacyBanner.swift` (the privacy disclosure banner) is *content*, not color — out of this plan's scope (flag for a separate cleanup). The distinct `FirstRunPermissionsView.swift` (the permissions/onboarding flow) **is** in scope for U4 styling — they are two different files.

### Deferred to Follow-Up Work

- Full migration of the remaining ~164 color literals and all non-visible surfaces (R9): separate PR after launch.
- Dedicated dark-mode / high-contrast craft pass (R9): separate PR after launch — this plan only ensures the four launch surfaces resolve correctly, not bespoke dark treatment everywhere.
- Generalized shared view-modifier / component vocabulary (R5 fast-follow half): separate PR.
- Recording-instrument icon artwork (#3), local-first trust indicator (#5), photo-library archive (#7): separate brainstorm/plan tracks.

---

## Context & Research

### Relevant Code and Patterns

- `macos/project.yml` — XcodeGen manifest. Deployment target macOS 13.0; `Assets.xcassets` is a target resource; app icon set via `ASSETCATALOG_COMPILER_APPICON_NAME`. The global accent is set here (`ASSETCATALOG_COMPILER_GLOBAL_ACCENT_COLOR_NAME`), then `xcodegen generate` regenerates the project — do **not** hand-edit `ScreenCap.xcodeproj/project.pbxproj`.
- `macos/ScreenCap/Assets.xcassets` — currently only `AppIcon.appiconset`; no color sets. Asset-catalog color sets natively support Any/Dark appearance + High Contrast variants, which resolve automatically (no `colorScheme` branching) — this is what makes R2's "derive from roles" feasible on the 13.0 target.
- `macos/ScreenCap/Views/Privacy/PrivacyBadgeStyle.swift` — the only existing style abstraction; `color` returns `.red` / `.orange` / `.secondary` for `blockedBySecurity` / `excludedByUser` / `captured`. The seed to generalize (R5).
- Launch-surface files: `macos/ScreenCap/ScreenCapApp.swift` (singleton `Window`, `MenuBarLabel` with `record.circle` + orange attention dot), `macos/ScreenCap/Views/RecordingBanner.swift` (pulsing red dot + elapsed + Stop), `macos/ScreenCap/Views/MainWindow.swift` (`NavigationSplitView`, `.orange` usages, `controlBackgroundColor` cards), `macos/ScreenCap/Views/Privacy/FirstRunPermissionsView.swift` (first-run; `.orange`, hardcoded `.font(.system(size:))`).
- 12 `.accentColor` / `.tint` call sites across `CalendarView.swift`, `SearchDayTimeline.swift`, `TimelinePane.swift`, etc. — all inherit the global `AccentColor` with zero edits.
- macOS app tests live in `macos/ScreenCapTests/`, run via `xcodebuild` (XcodeGen project). The Python CI lane (`pytest -m privacy`) does not cover the Swift app.

### Institutional Learnings

- `docs/solutions/.../swiftui-windowgroup-vs-window-singleton-scene-*.md` — the main window is a singleton `Window`; styling composes on it via overlays/sheets, not new windows. No new scenes introduced here.
- `docs/solutions/.../macos-ad-hoc-signing-tcc-rebuild-treadmill.md` — ad-hoc dev builds drop TCC grants on rebuild; use `tccutil reset` for a clean first-run when visually QA'ing the onboarding surface. Relevant to U4 verification, not behavior.

### External References

- Asset-catalog color sets resolve Light/Dark + High-Contrast variants automatically on macOS 13+ (confirmed during doc review). Global accent via `ASSETCATALOG_COMPILER_GLOBAL_ACCENT_COLOR_NAME` propagates to all `Color.accentColor` / `.tint` sites.
- Accessibility: amber/warm fills require dark text to hold WCAG 4.5:1; recording (amber) vs error (red) must differ by shape, not hue alone (red-green colorblindness). Neon gradient endpoints fail contrast on white → light mode uses a deepened gradient.

---

## Key Technical Decisions

- **Hybrid token mechanism (resolves the origin's "Deferred to Planning" mechanism question).** Colors → asset-catalog color sets surfaced through a thin `Color` extension; spacing/radius/type → a Swift token enum; the Aurora gradient → a SwiftUI `LinearGradient` constant. Rationale: color sets give light/dark/high-contrast resolution *for free* (meets R2 with no Theme/Environment plumbing or manual `colorScheme` branching), while spacing/type cannot live in the catalog so they need the enum. Simplest mechanism meeting all constraints.
- **Global `AccentColor` for the solid accent.** Satisfies R6's "one-place edit propagates everywhere" and the half-migration mitigation (untokenized surfaces inherit the new accent) with zero call-site edits across the 12 tint sites.
- **Aurora is gradient-on-hero + solid-for-tint.** SwiftUI's system tint is a single color, so the gradient is used only on the brand/menubar mark and the primary action; the derived solid spring-teal drives selection/links/focus. Light mode uses a deepened gradient (neon-on-white fails contrast).
- **Recording vs error separated by shape.** Recording = warm-amber filled dot/ring; error = red triangle/exclamation — so the two survive colorblindness and the "scarce color" rule (advisories neutral) holds.
- **Token names fixed now, values tuned at implementation.** The hexes in the origin (R6/R7) are illustrative; final values are tuned in OKLCH and contrast-checked when authoring the color sets.

---

## Open Questions

### Resolved During Planning

- SwiftUI token mechanism → hybrid (asset-catalog colors + Swift enum metrics/type + gradient constant). See Key Technical Decisions.
- Where the global accent setting lives → `macos/project.yml` (XcodeGen), regenerated via `xcodegen generate`.
- Which views back each launch surface → enumerated in Context & Research and U4.

### Deferred to Implementation

- Final OKLCH-tuned hex values for every color set (light/dark/high-contrast triplets).
- Whether the 16pt menubar glyph renders the Aurora gradient legibly or should resolve to the solid spring-teal at that size — decide by eye during U4.
- Final pressed-opacity value and exact disabled treatment for `AuroraButtonStyle` (resting/pressed/disabled are specified directionally in U3; tune by eye).
- Whether any additional interactive role (e.g. `overlayScrim`) gains a launch consumer once U4 is underway — stubbed in U1, promote to an authored color set if a surface needs it.

---

## Implementation Units

### U1. Color role tokens — asset catalog + global accent + Color extension

**Goal:** Author the semantic color roles as asset-catalog color sets (with light/dark/high-contrast variants), wire the global `AccentColor`, and expose roles + the Aurora gradient through a `Color` extension.

**Requirements:** R1, R2, R6, R7

**Dependencies:** None

**Files:**
- Create: `macos/ScreenCap/Assets.xcassets/AccentColor.colorset/Contents.json` (the derived solid spring-teal, light + dark)
- Create: per-role `macos/ScreenCap/Assets.xcassets/SC*.colorset/Contents.json` (each Any/Dark + High-Contrast). **Author color sets only for roles with a confirmed launch-surface consumer**; name-and-stub the rest in `SCColor.swift` so the contract is fixed without bloating the catalog.
  - **Author now (launch-consumed):** `recording`; the split signal roles `errorFg` / `errorSurface`, `advisorySurface` (advisory foreground = `textSecondary`, per R7's de-color), `successFg`; structural `surface`, `surfaceElevated`, `textPrimary`, `textSecondary`; interactive `selection`, `backgroundHover`, `backgroundPressed`, `disabledOnAccentFg` (Stop button states).
  - **Name-and-stub for R9 (no launch consumer yet):** `border`, `separator`, `textDisabled`, `focusRing`, `overlayScrim` — declare in `SCColor.swift` as `// TODO(R9): author color set` without creating the asset files.
  - Note the fg/surface split is the origin R2 contract (`state-error-fg`/`state-error-surface`, `state-advisory-fg`/`state-advisory-surface`); MainWindow's `.red.opacity` / `.yellow.opacity` overlays are *surface* uses and the icon tints are *fg* uses, so both halves are genuinely consumed at launch.
- Create: `macos/ScreenCap/Theme/SCColor.swift` (a `Color` extension exposing each role; the `AuroraButtonStyle`; and **two** gradient constants — `auroraGradient` (dark/neon) and `auroraGradientLight` (deepened, for light backgrounds). Call sites branch on `@Environment(\.colorScheme)` for the gradient — see the contradiction note below.)
- Modify: `macos/project.yml` (add `ASSETCATALOG_COMPILER_GLOBAL_ACCENT_COLOR_NAME: AccentColor` under the target settings)
- Test: `macos/ScreenCapTests/SCColorTests.swift`

**Approach:**
- Author color sets with Universal + Dark appearances and the High Contrast variant; values from the resolved Aurora palette (R6/R7), OKLCH-tuned + contrast-checked.
- The `Color` extension is the ergonomic seam (`Color.scSurface`, `Color.scRecording`, …) so call sites never touch raw asset names.
- **Gradient branching caveat:** the "no `colorScheme` branching" rationale (Key Technical Decisions) applies to *asset-catalog color sets only* — they auto-resolve. The two `LinearGradient` constants live outside the catalog and therefore *do* require a `@Environment(\.colorScheme)` branch at the hero call sites (the primary button, the in-window brand mark). This is the one intentional exception.
- After editing `project.yml`, regenerate with `xcodegen generate` (execution-time step).

**Patterns to follow:** mirror the existing `AppIcon` asset wiring in `project.yml` (`ASSETCATALOG_COMPILER_APPICON_NAME`).

**Test scenarios:**
- Happy path: each authored `Color.sc*` role resolves to a non-nil color in both light and dark `ColorScheme` (resolved cgColor non-nil).
- Edge case: high-contrast variant differs from the standard variant for at least the text and accent roles.
- Accessibility: `recording` paired with its on-fill text token achieves ≥ 4.5:1 contrast, and `errorFg` ≠ `errorSurface` (guards the split from re-collapsing) — assert at the authored values via `NSColor` contrast computation.
- Verification that the global accent name is set so `Color.accentColor` resolves to the new accent, not system blue.

**Verification:**
- App builds via `xcodebuild` after `xcodegen generate`; `Color.accentColor` renders as the spring-teal (not system blue) on an existing tinted control; authored roles resolve in both appearances.

---

### U2. Spacing, radius, and type tokens

**Goal:** Define the named spacing ramp, radius steps, and type scale as a Swift token namespace, encoding the "calm instrument" character constraint.

**Requirements:** R3, R4

**Dependencies:** None

**Files:**
- Create: `macos/ScreenCap/Theme/SCMetrics.swift` (spacing ramp `space1…spaceN`, radii `radiusSm/Md/Lg`)
- Create: `macos/ScreenCap/Theme/SCTypography.swift` (named steps `display`, `title`, `body`, `labelPrimary`, `labelSecondary`, `metadata`, `monoTimer` mapped onto system `Font` + Dynamic Type)
- Test: `macos/ScreenCapTests/SCMetricsTests.swift`

**Approach:**
- Spacing ramp calibrated toward the relaxed end (not compact); radii structural (not pill/capsule) by default, capsule reserved for the single primary action.
- Type steps map to system text styles / Dynamic Type categories; `monoTimer` carries `monospacedDigit()` for the elapsed timer.

**Patterns to follow:** existing semantic font usage (`.headline`, `.caption`) — the scale names those, it does not replace the system font.

**Test scenarios:**
- Happy path: spacing ramp values are strictly increasing (monotonic) and radius steps are ordered sm < md < lg.
- Edge case: the type scale exposes exactly the named steps and each maps to a non-nil `Font`.

**Verification:**
- Token namespace compiles and is referenced by U4 without magic numbers reappearing on the launch surfaces.

---

### U3. Styling seam — PrivacyBadgeStyle → roles + minimal launch components

**Goal:** Point the one existing style abstraction at the new roles, and add the single shared button style the launch surfaces need. Keep the component vocabulary out (it's R5 fast-follow).

**Requirements:** R5 (launch half = `PrivacyBadgeStyle` only), R7

**Dependencies:** U1

**Files:**
- Modify: `macos/ScreenCap/Views/Privacy/PrivacyBadgeStyle.swift` (map `blockedBySecurity` → `errorFg`, `excludedByUser` → advisory (text-only: foreground `textSecondary`, no distinct fill), `captured` → `textSecondary`)
- Test: `macos/ScreenCapTests/PrivacyBadgeStyleTests.swift`

**Approach:**
- **No `SCComponents.swift`.** Creating a named components module is the R5 component-vocabulary work the plan's own Deferred list defers — don't start it. `AuroraButtonStyle` lives in `SCColor.swift` (U1) alongside the gradient it consumes; the recording indicator is an inline `Circle().fill(Color.scRecording)` at its one call site in `RecordingBanner.swift` (U4), not an extracted view.
- `PrivacyBadgeStyle` is a pure `kind → role` mapping — directly unit-testable. The advisory badge de-colors to **text-only** (background stays `scSurface`, text `scTextSecondary`) — there is no advisory *fill*, so the implementer must not reach for an undefined `scAdvisory` background.
- `AuroraButtonStyle`: resting = the colorScheme-appropriate Aurora gradient with a **dark** label token (not `.primary`/`.white`); pressed = gradient at ~0.85 opacity; disabled = flat `scSurface` with `disabledOnAccentFg` label (no gradient); corner radius = the capsule radius (the one place capsule is allowed per R4).

**Patterns to follow:** the existing `PrivacyBadgeStyle` struct shape (a `var color` switch) — extend, don't rewrite. Note: the existing `PrivacyBadgeStyleTests` covers `kind`/`text`/`tooltip`, **not** `color` — so the role assertions below are net-new coverage added to that file, not edits to existing asserts.

**Test scenarios:**
- Happy path: `PrivacyBadgeStyle` maps each kind to its expected role (`blockedBySecurity` → errorFg, `excludedByUser` → textSecondary, `captured` → textSecondary).
- Edge case: `excludedByUser` no longer returns a raw `.orange` (guards the de-color decision).
- Accessibility: `AuroraButtonStyle`'s label foreground resolves to a dark token (assert it is not `.white`/`.primary`) so dark-text-on-amber/lime contrast holds.
- Note: recording-vs-error *shape* distinction is **not** snapshot-assertable here — the repo's `SearchViewHostingHarness` cannot read SwiftUI colors or shapes back — so it is verified in U4's manual light/dark gate, not by an automated shape test.

**Verification:**
- Privacy badges render with the new roles (advisory is text-only); the Stop button uses `AuroraButtonStyle` with legible resting/pressed/disabled states in both appearances.

---

### U4. Apply tokens to the four launch-visible surfaces + dark-validation gate

**Goal:** Replace inline literals with tokens on the menubar mark, recording banner, main-window chrome/sidebar, and first-run onboarding; apply the Aurora accent (gradient hero / solid tint) and the resolved state colors; then run the explicit light/dark validation gate.

**Requirements:** R8

**Dependencies:** U1, U2, U3

**Files:** (each bullet lists the *specific* literals to retire — the "no literals remain" test depends on naming them all)
- Modify: `macos/ScreenCap/ScreenCapApp.swift` (`MenuBarLabel` — `Color.orange` attention dot dropped; recording glyph `Color.red` → `Color.scRecording`; status-item glyph adopts the accent. **Realistically the menubar glyph renders as a template image (system-tinted), so it will take the solid accent via `Color.accentColor`/`scAccent`, not the gradient** — treat gradient-on-menubar as speculative, solid as the default.)
- Modify: `macos/ScreenCap/Views/RecordingBanner.swift` (recording dot `Color.red` → inline `Circle().fill(Color.scRecording)`; Stop button `.tint(.red)`/`.borderedProminent` → `AuroraButtonStyle`; spacing/radius tokens)
- Modify: `macos/ScreenCap/Views/MainWindow.swift` (sidebar selection = `scSelection`; **error overlay `.red.opacity(0.15)` → `Color.scErrorSurface`; advisory overlay `.yellow.opacity(0.18)` → neutral `scSurfaceElevated` (per R7 de-color, *not* yellow); icon `.orange` → advisory/state fg; toolbar `.tint(.red)` Stop → accent/AuroraButtonStyle**; cards `controlBackgroundColor` → `surfaceElevated`; spacing/radius tokens)
- Modify: `macos/ScreenCap/Views/Privacy/FirstRunPermissionsView.swift` (**ad-hoc-build callout `.orange` icon + `.orange.opacity(0.12)` background → advisory roles; the `daemonInstallIconColor` switch `.green`→`successFg` / `.red`→`errorFg` / `.orange`→advisory fg; "Granted" `.green` label → `successFg`**; replace `.font(.system(size: 16/22/20))` literals with type-scale steps)
- Test: `macos/ScreenCapTests/LaunchSurfaceTokenTests.swift` (source-level grep guard) + the manual light/dark gate below

**Approach:**
- "Applied" = no inline color literals remain on these four surfaces and role references are in place (per R8). The MainWindow and FirstRunPermissions literals above are easy to miss — they are the reason this list is exhaustive rather than "the `.orange` ones."
- Menubar glyph: solid spring-teal is the default (template-image rendering); the 16pt gradient is a speculative option to try by eye, not the plan of record.

**Execution note:** Apply the token swap before the visual gate — the gate validates the result, so it runs last within this unit, and only after U1's *final* color values are in place (not placeholder stubs).

**Patterns to follow:** the existing view structure on each surface — swap literals for tokens, do not restructure layout.

**Test scenarios:**
- Happy path (automatable): a source-level guard test asserts none of the four surface files contain `Color.red` / `.orange` / `.yellow` / `.blue` / a system-blue accent literal — this is deterministic and is the primary "no literals remain" check (SwiftUI color/shape cannot be read back from the test host).
- Edge case: recording banner uses `scRecording` (not red); the menubar attention dot is gone.
- Error path: an error chip (e.g. upload-failed, where present) uses `scErrorSurface`/`scErrorFg` with the triangle shape — distinct from the recording dot.

**Verification — dark-validation gate (binary, implementer-owned; a second-person glance recommended before the July 3 build; requires U1 final values, not stubs):**
- **Menubar:** accent visible in light + dark; no orange pip remains.
- **Recording banner:** amber recording dot (not red); Stop button gradient legible in both appearances; elapsed-time label readable.
- **Main window:** sidebar selection uses the accent; error/advisory overlays use the neutral/error roles (no raw `.yellow`/`.red`); cards use `surfaceElevated`.
- **First-run:** permission rows + daemon-install states use token colors; type uses named scale steps; amber/warm fills carry dark text.

---

## System-Wide Impact

- **Interaction graph:** the global `AccentColor` changes the tint on *all* `.accentColor` / `.tint` sites app-wide (12 sites) — intended (prevents the designed-next-to-stock split), but means non-launch surfaces visibly change accent too. No behavior change.
- **API surface parity:** none — this is presentation only; no public API, CLI, or daemon contract is touched.
- **State lifecycle risks:** none — no persistence, migration, or data path involved.
- **Unchanged invariants:** layout/structure of every surface is preserved; only color/spacing/type/styling change. The singleton-`Window` scene model, recording pipeline, and privacy enforcement are untouched.
- **Integration coverage:** SwiftUI rendering is hard to unit-test; the light/dark gate (U4) is the cross-cutting check that mocks won't prove.

---

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| Menubar status-item glyph renders as a template image and ignores `foregroundStyle(gradient)` | Plan of record is the solid spring-teal on the menubar (via the global accent); gradient-on-menubar is a speculative by-eye option, not assumed to work |
| Neon gradient fails contrast on white (light mode) | Deepened light-mode gradient authored in U1; dark-validation gate also checks light |
| `project.yml` edited but project not regenerated → global accent not applied | U1 verification requires `xcodegen generate` + a build before the accent is confirmed |
| Half-migration (4 surfaces tokenized, rest stock) reads inconsistent | Global `AccentColor` makes untokenized surfaces inherit the accent + dark resolution, narrowing the gap |
| The repo's SwiftUI test host (`ScreenCapTests/SearchViewHostingHarness.swift`) cannot read back a `Color` or distinguish shapes | Automated checks lean on the pure-mapping unit tests (U1 role resolution, U3 `kind→role`) + a deterministic source-level grep guard for "no literals remain" (U4); color/shape correctness is the manual light/dark gate, by design |
| Ad-hoc dev rebuilds drop TCC grants while QA'ing onboarding | `tccutil reset` for a clean first-run (institutional learning) |

---

## Sources & References

- **Origin document:** [docs/brainstorms/2026-06-30-design-token-foundation-requirements.md](docs/brainstorms/2026-06-30-design-token-foundation-requirements.md)
- Color design pass + Aurora palette rationale: [docs/ideation/2026-06-30-visual-design-language-ideation.md](docs/ideation/2026-06-30-visual-design-language-ideation.md)
- Related code: `macos/project.yml`, `macos/ScreenCap/Assets.xcassets`, `macos/ScreenCap/Views/Privacy/PrivacyBadgeStyle.swift`, `macos/ScreenCap/ScreenCapApp.swift`
