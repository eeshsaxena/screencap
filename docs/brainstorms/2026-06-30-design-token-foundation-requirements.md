---
date: 2026-06-30
topic: design-token-foundation
origin: docs/ideation/2026-06-30-visual-design-language-ideation.md
---

# Design-token foundation for the macOS app

## Summary

A semantic design-token foundation for the ScreenCap macOS app: one source of truth for color, typography, spacing, and corner radius, plus a thin reusable styling layer, with roles that resolve per appearance. Defined fully, but applied before the July 3 launch only to the high-traffic visible surfaces; the full color-ref migration and dark-mode craft pass are fast-follow.

---

## Problem Frame

The macOS app is essentially stock SwiftUI. Color lives as ~164 scattered inline literals with no token or theme file; the accent is the default system blue (the same as every unstyled SwiftUI app); typography is raw system styles with no scale; spacing is hardcoded (16/12/20/14) and corner radius is a magic 10, with no grid; there is no reusable styling layer beyond a single isolated `PrivacyBadgeStyle`; and `.orange` is overloaded for both warnings and benign advisories, so a user cannot tell "something needs you" from "FYI." There is no dark-mode craft.

The cost shape: every visual change re-litigates pixel values across many call sites and the app drifts back toward an inconsistent stock look; credible dark-mode and high-contrast are effectively unshippable because there is no role layer to resolve them from. This lands at a load-bearing moment — the 2026-07-03 Product Hunt launch, where "UX & native experience" is the quarter's load-bearing track. For a non-technical buyer evaluating an always-on recorder for their team's machines, a stock-SwiftUI look is a trust *liability* at first contact — it reads as "a hack," raising the bar on the very privacy promise the product sells. The foundation's job is to remove that liability on the surfaces seen during install and first-run; it is defensive/table-stakes, not an independent claim that visual polish drives activation. This foundation is also the substrate the rest of the design direction ("a calm recording instrument that sits lightly on your Mac") depends on: the Liquid Glass chrome, the recording-instrument identity, the warmth-not-alarm recording state, the local-first trust indicator, and the premium archive all consume these tokens.

---

## Requirements

**Token system**
- R1. A single source of truth defines tokens for color, typography, spacing, and corner radius; views consume tokens rather than inline literals.
- R2. Color tokens are semantic / role-based. The named role set (names fixed now, values deferred to the design pass) covers at minimum: `surface`, `surface-elevated` (inset cards/panels), `border`, `separator`; `text-primary`, `text-secondary`, `text-disabled`; `accent`; `focus-ring`, `selection` (driven by the accent); `state-recording`; and the signal states split into foreground vs. fill — `state-error-fg` / `state-error-surface`, `state-advisory-fg` / `state-advisory-surface`, `state-success-fg`. Light, dark, and high-contrast are resolutions of the same roles — not separately hand-maintained palettes; the mechanism chosen in planning must support automatic high-contrast resolution (system semantics or asset-catalog variants), not a separately authored palette.
- R3. Typography is a named type scale on the system font + Dynamic Type (no custom typeface), with named steps (values deferred): e.g. `type-display`, `type-title`, `type-body`, `type-label-primary`, `type-label-secondary`, `type-metadata`, `type-mono-timer`. Existing semantic styles (`.headline`, `.caption`, …) are already Dynamic-Type-compliant; for the launch slice (R8) only the raw `.font(.system(size:))` literals on the four visible surfaces are replaced — full type-token migration of other surfaces is R9 fast-follow.
- R4. Spacing and corner radius are named token ramps with no magic numbers (step names fixed now, values deferred); density is a property of the system, tunable from one place. Character constraint: the spacing ramp calibrates toward the *relaxed* end of the native macOS range (not compact), and the radius ramp uses *structural* radii (not pill/capsule) by default, with capsule reserved for the single primary action — so the tokens express the "calm instrument" identity rather than a neutral template. For the launch slice (R8) the ramps are *applied* to the four visible surfaces; remaining surfaces are R9 fast-follow (defining the ramp already satisfies "tunable from one place").
- R5. (Launch) Update `PrivacyBadgeStyle` to consume the color token roles rather than inline literals. (Fast-follow, R9) Generalize a shared view-modifier / component vocabulary on top of the tokens so common surfaces are composed rather than re-styled per view — this broader abstraction is not required for the launch slice.

**Color identity & state semantics**
- R6. **Resolved — the "Aurora" signature accent** replaces the default system blue. The accent is a **lime→aqua duotone gradient** (illustrative `#C6F23D` → `#1FE3D2`) used only on hero moments — the brand / menubar mark and the single primary action — while a **derived solid spring-teal** (illustrative `#23CFA0` dark / deepened `#0E9E84` light) drives the system tint (selection, links, focus), since SwiftUI's accent is a single color. It is cool green-cyan, deliberately distinct from the warm recording signal, and chosen to **stand out on dark** (the gradient glows on near-black; light mode uses the deepened variant to hold WCAG contrast on white). Exact values are tuned in OKLCH and contrast-checked at implementation.
- R7. The overloaded `.orange` is retired into distinct, single-meaning state roles, and red is reserved for a single meaning. Note: some current `.orange` uses are *categorical*, not advisory — the timeline event-type legend (`TimelinePane` / `EventContentPane`) colors "screen event," which maps to a chart/category palette, **not** the warning/advisory state roles. For the launch slice (R8), orange is retired only on the four visible surfaces; other surfaces (Search, Inspect, Privacy panes) are R9 fast-follow. **Resolved state model:** recording-active = a calm **warm amber** (illustrative `#F0A12B`); **red is reserved strictly for error/critical** (illustrative `#E5484D`); **advisories de-color to neutral** (text-secondary) — which is what removes the overload; **success = a soft green** (illustrative `#3DC98A`). Recording and error are distinguished by **shape** (filled dot/ring vs. triangle/exclamation), not hue alone, so they survive red-green colorblindness; warm/amber fills use dark text to hold contrast.

**Launch scope**
- R8. For July 3, the token system is defined and applied to the launch-visible surfaces: the menu-bar status-item icon glyph (the accent applied to the status-item image in `ScreenCapApp.swift`; the dropdown items carry no color literals to migrate), the recording banner, the main-window chrome / sidebar, and first-run onboarding. The signature accent ships as a **global asset-catalog `AccentColor`** so even untokenized surfaces (Search, Review, Privacy) inherit the new accent and its dark resolution — preventing a "designed-next-to-stock" split during the slice. These surfaces must render correctly in both light and dark; because the app has no dark-mode handling today, dark correctness is gated by explicit validation — each of the four surfaces eyeballed in dark *after* the accent lands, against the design-pass cutoff (see Dependencies). "Applied" means the role references are in place and no inline color literals remain on these four surfaces; the final hue values for the accent and signal-state roles are filled in after the design pass but before the July 3 build — token wiring is not blocked by the design pass, only the final values are.
- R9. Full migration of the remaining color references and non-visible surfaces, and a dedicated dark-mode / high-contrast craft pass, are explicitly fast-follow after launch — scaffolded by the role system but not required for the launch slice.

---

## Success Criteria

- The July 3 build reads as deliberately designed — not stock SwiftUI — on the visible surfaces: a distinct accent, coherent spacing and type, and correct rendering in dark mode.
- Changing the accent or any semantic color is a one-place edit that propagates everywhere the role is applied (no N-site hunt).
- On the four launch-visible surfaces (R8), no color is ambiguous or overloaded — each signal color carries exactly one meaning. The same guarantee extends to all surfaces after the R9 migration.
- Downstream handoff: ce-plan can choose the SwiftUI mechanism and migration order without inventing which tokens exist, what the semantic roles are, or what is launch-critical vs fast-follow.

---

## Scope Boundaries

- The other five design-direction survivors — Liquid Glass chrome, recording-instrument icon artwork, recording-state motion, the local-first trust indicator, and the photo-library archive — consume these tokens but are separate work. The icon and the warm recording state appear here only as *inputs* to the accent/state decision, not as work in this doc.
- Exact accent hex and the final state-palette semantics — a design-pass decision, not invented here.
- Implementation mechanism (SwiftUI Environment values vs asset catalog vs a Theme type) and migration tooling — ce-plan's job.
- Full migration of all ~164 color references and all non-visible surfaces — fast-follow, not launch.
- A custom/brand typeface — out of scope; system font with a defined scale only.
- Liquid Glass / macOS 26 SDK adoption — independent of this foundation; the token layer must not block on that build-target decision.

---

## Key Decisions

- **Visible slice now, full migration after.** Fits the 3-day runway, lets the launch build look credibly designed where the buyer actually looks, and unblocks the rest of the direction — without betting the launch on migrating every call site.
- **Derive dark / high-contrast from semantic roles, not parallel palettes.** The only way to ship credible dark-mode parity by launch without a separate hand-painted pass, and it prevents light/dark drift permanently.
- **Defer the accent hue and state semantics; fix only the criteria.** The exact values are a taste/design-pass decision that must cohere with the recording-instrument icon and the warm recording state; picking them now, before that design pass, would be premature. The doc constrains them instead.
- **Keep the token layer independent of Liquid Glass.** Avoids coupling the foundation to the macOS 26 SDK / build-target risk decision, so the foundation can land regardless of when Glass is adopted.
- **A bold, ownable signature over maximal restraint.** The accent direction deliberately moved from "calm / low-profile" to the vivid Aurora gradient that stands out on dark — the goal is a distinctive, memorable identity, not a quiet one. Discipline keeps it controlled: the gradient appears only on hero moments, everything else stays neutral, and the warm recording signal + reserved red are unchanged. This updates the ideation doc's "a calm recording instrument that sits lightly" north-star toward **"a distinctive instrument with a signature glow."**

---

## Alternatives Considered

- **Cheap path — global accent swap + `.orange` fix only.** Ship a global asset-catalog `AccentColor` (propagates app-wide, including untokenized surfaces) plus the `.orange` overload fix, and defer the whole token foundation to fast-follow. This buys most of the "not stock blue" perception for a fraction of the work; its ceiling is lower (no type/spacing/role layer for the other survivors to consume). **Infra-first beats this only if** dark-mode parity across the slice is launch-critical, the accent must propagate identically across all four surfaces *and* the fast-follow surfaces, and the other design-direction survivors (which consume the roles) start immediately after launch. If those don't hold, the cheap path is the better launch-week bet and this decision should flip.
- **Hand-polish the four surfaces inline, tokenize post-launch.** Rejected for the same reason — it ships the same visible result only if dark-mode parity isn't needed, and leaves no role layer for the rest of the direction.

---

## Dependencies / Assumptions

- A macOS app build ships at or around the 2026-07-03 launch (the app was recently bumped to 0.1.1). Verified against the repo: the app exists at `macos/ScreenCap` and no token / theme / palette file exists today.
- The accent + state-palette are now **settled** (R6/R7 — Aurora, 2026-06-30). The recording-instrument icon (#3) should be designed to harmonize with the Aurora palette, but it does not block the restyle.
- **Critical path (mostly retired 2026-06-30):** the accent + state-palette decisions that gated R8 are resolved, so the earlier "provisional fallback accent" is moot. The remaining design-pass dependency is the recording-instrument icon artwork (#3, longest lead time); if it slips, R8 still ships the resolved Aurora palette. A code-freeze / notarization cutoff (below) is now the main schedule risk, not the color decision.
- **Build cutoff:** set a code-freeze / notarization cutoff for launch-affecting visual changes working backward from 2026-07-03 (the macOS build needs Developer ID signing + notarization lead time); confirm the restyle + late accent fit inside it.
- Assumes the system font + Dynamic Type is acceptable for launch (no brand typeface in scope).

---

## Outstanding Questions

### Resolved in the design pass (2026-06-30)

- [R6] Signature accent — **Aurora** (lime→aqua gradient + derived solid spring-teal). See R6.
- [R7] State-palette semantics — warm-amber recording, red = error only, advisories de-colored to neutral, success soft green, recording-vs-error distinguished by shape. See R7.

No Resolve-Before-Planning items remain — the visible-surface restyle (R8) is no longer blocked on a pending design decision.

### Deferred to Planning

- [Affects R1–R5][Technical] The SwiftUI token mechanism (Environment values, asset catalog color sets, or a Theme type) and the migration order across surfaces — decide during planning against the codebase.
- [Affects R8][Technical] Which exact views back each launch-visible surface, and the per-surface migration sequence — codebase exploration during planning.
