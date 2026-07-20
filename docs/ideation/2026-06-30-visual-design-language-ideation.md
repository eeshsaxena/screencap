---
date: 2026-06-30
topic: visual-design-language
focus: Visual & interaction design language (look/feel/craft) of the macOS SwiftUI app for the 2026-07-03 Product Hunt launch — typography, color, layout/density, motion, material/depth, menubar+app-icon identity, visual trust expression. NOT feature ideation.
mode: repo-grounded
---

# Ideation: Visual & interaction design language for Screencap's macOS app

**Design direction (north-star):** *"A calm recording instrument that sits lightly on your Mac."* Native-Tahoe glass (you see your own desktop through a thin layer) + one coherent instrument identity expressed from Dock → menubar → live indicator + warmth-not-alarm + wordless local-first trust + a premium archive. Disciplined, scarce color; minimal honest motion; the Things/Fantastical craft tier in a category that currently has no design-forward player.

> **Update (2026-06-30, color design pass):** the accent direction evolved from "calm / scarce color" toward a **bold, ownable signature** — the **"Aurora" lime→aqua gradient** — chosen to stand out (especially on dark). The discipline holds (gradient on hero moments only; warm-amber recording; red reserved for error; advisories neutral), but the north-star is better read as *"a distinctive instrument with a signature glow"* than "sits lightly." Resolved palette captured in `docs/brainstorms/2026-06-30-design-token-foundation-requirements.md` (R6/R7).

## Grounding Context (Codebase Context)

- **Current visual state (UI scan):** Essentially stock SwiftUI. Singleton `Window` main + `WindowGroup` review/inspect scenes. Sidebar: Calendar / Recordings / Search / Privacy. RecordingBanner = pulsing red dot + elapsed + Stop. Menubar = `record.circle` (red while recording) + 5px orange attention dot.
  - **Color:** semantic only (.primary/.secondary/.red/.orange/.tint), **164 scattered inline refs, NO token/theme file**, **default system-blue accent**, no dark-mode craft.
  - **Typography:** system styles only, **no type scale**, no custom font.
  - **Iconography:** SF Symbols only; **stock app icon** (no custom artwork).
  - **Spacing:** hardcoded (16/12/20/14), radius 10, **no tokens/grid**.
  - **Motion:** only the recording red-dot opacity pulse; no hover/press/spring/transition language.
  - **No design system / reusable ViewModifiers / vibrancy / depth.** Only an isolated `PrivacyBadgeStyle` struct.
  - **Named gaps:** raw CLI string "screencap setup" leaked into `FirstRunPrivacyBanner`; `.orange` overloaded (warning vs advisory); weak review-window hierarchy; sidebar fixed-expanded; no reduced-motion/high-contrast variants.
- **Constraints (learnings — bound feasibility):** singleton `Window` → new surfaces are sheets/overlays/own scenes; **TCC caches per-process → Quit-&-Relaunch is an unavoidable onboarding beat** (Loom/1Password precedent); any "skip/later" needs an always-reachable reopen affordance; recorder telemetry is **coarse event-driven over pipes → motion must be gap-tolerant**, no fast-poll, no per-frame-telemetry UI; review timing fields nullable → first-class empty/zero-duration states required. The design language itself is **greenfield** — zero prior learnings on type/color/spacing/motion/icon/brand.
- **External context (web):** macOS **Tahoe 26 "Liquid Glass"** is the current native baseline — translucent material on the NAV layer only, `.glassEffect`/`GlassEffectContainer`, `.glassProminent` on the *single* primary action; recompiling updates chrome for free. **Things 3** (sparing single accent, text-primary, relaxed spacing, larger radii, glass-tinted sidebar, minimal purposeful animation, no in-app marketing chrome). **Fantastical** (micro-interaction craft; neutral chrome + one vivid color does the semantic work). **Raycast** (behavioral-authenticity test; no web-like hover/cursor). **CleanShot X** (menubar icon *is* the identity — monochrome template, crisp at 16pt). **Signal / DuckDuckGo** (trust via persistent low-prominence in-flow indicator, not a banner/pane). Competitors: **Screenpipe** rough/dev-first; **Rewind** dead (local-first as a text tagline alone proved fragile); **Timing/Rize** web-influenced with a native-feel gap → the Things/Fantastical tier is an **open lane**. Cross-domain: black-box/dashcam (indicator light, not a viewport; no live preview), calm-tech (interrupt only on failure, else ambient), photo-library (Review = "your screen's photo library," not a surveillance log).
- **Strategy anchor (STRATEGY.md):** buyer = **non-technical small-business owner**; **2026-07-03 Product Hunt launch**; "UX & native experience" is **load-bearing this quarter**. Brand: "everything stays on your own machines," "turn your best employee's workflow into everyone's playbook." Must feel premium, native, calm, trustworthy — not enterprise-sterile, not surveillance-y.

## Topic Axes

- **A. Foundation system** — color palette, type scale, spacing/density, corner radius, dark-mode tokens.
- **B. Material, depth & native chrome** — Liquid Glass alignment, vibrancy, window/sidebar/toolbar treatment.
- **C. Brand & ambient identity** — app icon, menubar-as-product-face, signature accent, "recording-instrument" identity.
- **D. Motion & micro-interactions** — transitions, hover/press feedback, recording-state motion, delight (gap-tolerant).
- **E. Trust expression (visual)** — glanceable local-first/recording state, persistent calm indicators, show-don't-tell.

## Ranked Ideas

### 1. Design-token foundation + semantic color discipline
**Description:** Collapse the 164 scattered color literals, hardcoded spacing (16/12/20/14), radius (10), and raw system type into one token source (color/space/radius/type) plus a thin ViewModifier/component layer. Derive dark-mode and high-contrast as *resolutions of semantic roles*, not hand-painted second palettes. Split the overloaded `.orange` into a 3-tier state palette (calm / advisory / attention) where each hue means exactly one thing; reserve red strictly for active-recording + true error.
**Axis:** A
**Basis:** `direct:` "164 scattered inline color refs, NO token/theme file"; "spacing hardcoded (16/12/20/14), radius 10, no tokens"; "typography system styles only, no scale"; "`.orange` overloaded (warning vs advisory)."
**Rationale:** The greenfield substrate every other move depends on; turns each future polish from an N-site hunt into a one-file diff, and is the only way to ship credible dark-mode/high-contrast craft by July 3.
**Downsides:** Refactor touches many files; bikeshedding risk on the scale; must sequence first or later ideas cost more.
**Confidence:** 95%
**Complexity:** Medium
**Status:** Explored

### 2. Adopt Liquid Glass as the native (and trust) chrome
**Description:** Build against the macOS 26 SDK so toolbars/sidebar/sheets take Liquid Glass for free; glass on the nav layer only (never content), recording controls in a `GlassEffectContainer`, a single `.glassProminent` per surface (Start/Stop). Frame the translucency as *trust* — you see your own desktop through a thin layer, "this app sits lightly on your machine." Depth seats controls (cockpit-bezel discipline), never decorates.
**Axis:** B
**Basis:** `external:` Tahoe 26 Liquid Glass (recompile = free chrome; `.glassProminent` on the single primary action) + `direct:` "no vibrancy/shadows/depth."
**Rationale:** A flat non-vibrant window is the fastest "not really native" tell to a Mac-literate PH audience; matching the OS material is near-free and closes the Timing/Rize native-feel gap that is the open lane.
**Downsides:** Gating on the 26 SDK is a build-target/risk call; glass-on-content violates HIG; verify menubar/contrast against Tahoe rules.
**Confidence:** 80%
**Complexity:** Low-Medium
**Status:** Unexplored

### 3. One "recording-instrument" identity — app icon + 16pt menubar glyph
**Description:** Commission a custom identity designed **16pt-first**: a monochrome template menubar mark with three honest states (idle outline / recording filled / attention pip), and a full-color Dock icon echoing the same geometry. Motif = an indicator light / aperture / tally — an instrument at rest, deliberately *not* a camera/eye/surveillance symbol. Drop the stock `record.circle` + appended orange dot.
**Axis:** C
**Basis:** `direct:` "STOCK app icon; menubar uses record.circle + orange attention dot" + `external:` CleanShot X (menubar icon AS identity, monochrome template crisp at 16pt), broadcast tally light.
**Rationale:** For a menubar-resident tool the 16pt mark is the most-seen pixels of the whole brand and the PH gallery thumbnail; stock art caps perceived quality no matter how good the UI is. **Longest lead time of anything here — must start now to make July 3.**
**Downsides:** Custom artwork needs a designer + iteration; the state system must stay legible at 16pt in light/dark/tinted menubars.
**Confidence:** 85%
**Complexity:** Medium
**Status:** Unexplored

### 4. Recast the recording-state signature: warmth not alarm, breathe not blink
**Description:** Replace the pulsing red dot with a calm, gap-tolerant signature — a single "settle and hold" transition at start (aperture/iris or soft fill), a slow breathing presence while live (driven by recording-state *events*, never per-frame telemetry), and a warm calm hue instead of alarm-red (red reserved for genuine error). Everything else stays still; reduced-motion drops to instant state with no loop, satisfied by construction.
**Axis:** D
**Basis:** `direct:` "motion = ONLY the recording red-dot opacity pulse; no reduced-motion" + constraint "telemetry coarse/event-driven → motion gap-tolerant" + `external:` calm-tech, dashcam settle-and-hold, Raycast (no web-like motion), Things minimal animation.
**Rationale:** The recording state is the most-seen state in the product and currently signals "alert/surveillance"; recasting it as calm warmth is the highest-leverage trust move, and a state-driven design is the only honest one given coarse telemetry.
**Downsides:** Warmth must not collide with the new accent or amber-advisory; "breathing" needs taste to avoid feeling alive/creepy.
**Confidence:** 85%
**Complexity:** Low-Medium
**Status:** Unexplored

### 5. Wordless, persistent local-first trust indicator (show-don't-tell)
**Description:** Express "everything stays on your machine" as a quiet, always-present in-flow mark (toolbar/menubar/sidebar footer) using a containment motif and the calm token — not a dismissible banner or a settings sentence. Govern with a calm-tech salience budget: silent at healthy baseline, escalating prominence only on real failure (permission lost, disk full). Kills the leaked CLI string in the `FirstRunPrivacyBanner`.
**Axis:** E
**Basis:** `external:` Signal/DuckDuckGo (persistent low-prominence in-flow indicator), calm-tech (interrupt only on failure), Rewind (text-tagline local-first was fragile) + `direct:` "raw CLI string 'screencap setup' leaked into FirstRunPrivacyBanner."
**Rationale:** The core brand promise is load-bearing for a non-technical buyer; a quiet persistent mark makes the guarantee felt on every screen and survives skim-reading/screenshots in a way a tagline doesn't.
**Downsides:** Must avoid security-theater; placement (toolbar vs menubar) is a cross-surface decision; needs the containment motif from #3 to feel coherent.
**Confidence:** 80%
**Complexity:** Medium
**Status:** Unexplored

### 6. Quit-&-Relaunch as a designed "arming the instrument" ceremony
**Description:** The TCC per-process cache forces a relaunch after granting Screen Recording. Instead of an error toast, design it as a confident, branded, full-bleed first-run beat — "arming the instrument / waking up ready" — with clear before/after and one obvious relaunch CTA (Loom/1Password precedent). Pair with always-reachable re-open affordances so a hasty "skip" during a demo isn't a dead end.
**Axis:** E (interaction / onboarding)
**Basis:** `external:` Loom/1Password relaunch-as-designed-beat + learnings: "TCC caches per-process → Quit-&-Relaunch unavoidable"; "any skip/later needs an always-reachable reopen affordance."
**Rationale:** First-run is the highest-stakes second of a PH funnel for a non-technical buyer; converting a forced restart into intentional craft flips the most likely "this feels janky" moment into a trust-builder.
**Downsides:** Must handle the rebuild-treadmill/edge states; messaging across two TCC subjects (app vs daemon) is non-trivial.
**Confidence:** 80%
**Complexity:** Medium
**Status:** Unexplored

### 7. Style the archive/Review surface as a premium photo-library, not a log
**Description:** Anchor a generous editorial type scale and large spacing tokens, and restyle Recordings/Review as a curated archive (large quiet titles, small tracked metadata captions, content-forward thumbnails, abundant whitespace, first-class empty/zero-duration states) — "your screen's photo library," not a surveillance log row-list. Density starts relaxed and rarely tightens.
**Axis:** A (type / density / positioning)
**Basis:** `external:` photo-library analogy, museum/archive label type, Things/Fantastical relaxed-spacing tier (the open lane) + `direct:` "weak review-window hierarchy"; "review timing nullable → must design empty states."
**Rationale:** Dense grids read as enterprise/surveillance/scary to a non-technical buyer; an airy curated archive reframes recordings as a respectful personal asset store — the perceptual shift the brand ("your best employee's playbook") needs, delivered through type and density rather than copy.
**Downsides:** Large net surface to restyle; at-scale (1000 recordings) density tradeoff later; depends on the type tokens from #1.
**Confidence:** 75%
**Complexity:** Medium-High
**Status:** Unexplored

## Through-line

#3 + #4 + #5 are *one instrument* expressed across surfaces (the menubar mark, its calm breathing, the local-first indicator). #1 + #2 are the substrate that makes it cheap and native. #6 + #7 are the two highest-stakes screens — first-run and the archive.

## Rejection Summary

| # | Idea | Reason Rejected |
|---|------|-----------------|
| 1 | Menubar IS the whole app / window is the darkroom | IA/architecture reframe, not visual-design language — carry into brainstorm as an open positioning question |
| 2 | One signal hue, everything else greyscale | Extreme palette variant; folded into #1 (scarce color) + #4 (recording hue) |
| 3 | Zero-settings face | IA/feature decision more than visual language; partial fold into #2 chrome restraint |
| 4 | Everything is glass, nothing is solid | Violates HIG (glass = nav layer only, never content); disciplined version folded into #2 |
| 5 | Density inversion / one thing per view | Extreme of #7; folded |
| 6 | No live preview — indicator light only | Folded into #4 + #5 |
| 7 | Patient-monitor calm baseline | Governing principle folded into #5 (salience budget) |
| 8 | Reduced-motion / high-contrast as variants | Folded into #1 (high-contrast tokens) + #4 (reduced-motion) |
| 9 | ViewModifier component library + token sub-ideas | Folded into #1 |
| 10 | Vinyl/hi-fi engraved local-first seal | Skeuomorph risk against a glass-native direction; brainstorm variant |
| 11 | Designed-at-16pt-first | Folded into #3 as the design method |
| 12 | Flight-instrument bezel | Folded into #2 (depth seats controls) |
| 13 | Replace system-blue with signature accent | Folded into #1 (accent is a foundation token) — surfaced prominently there |
