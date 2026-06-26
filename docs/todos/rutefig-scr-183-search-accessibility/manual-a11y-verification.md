# SCR-183 — manual VoiceOver + keyboard verification (pre-merge)

The label/announcement logic is unit-tested, but focus, key handling, the VoiceOver
announcement *posting*, and Dynamic Type layout have no unit-test seam in this project.
Run this checklist on a **macOS 13** machine with VoiceOver (⌘F5) before merge — it is the
acceptance artifact for R8 ("fully operable end-to-end with VoiceOver + keyboard"). Record
the macOS version it was run on in the PR.

## VoiceOver labels (U1, U2, U6)
- [ ] Coverage chips speak state in words, e.g. "On screen: 3 results", "Audio: no matches", "Activity: not indexed" — the color dot is not announced separately.
- [ ] Decorative icons are silent: no stop on the search magnifying glass, the consent viewfinder, the interpretation wand, or the lock.
- [ ] The consent banner reads as one sentence (heading + body), then "Not now, button" and "Turn on, button".
- [ ] Each result row is a single VoiceOver stop speaking app/snippet + time + stream (+ "approximate, from audio" for audio hits), then "button" — no separate stops for snippet/time/"≈ audio", and the thumbnail is silent.
- [ ] The search field announces as "Search your history".

## Non-result states + announcements (U6)
- [ ] `.searching` announces "Searching your history", not a bare "progress indicator".
- [ ] Empty / "Nothing recorded then" / idle / "ScreenCap isn't running" each read as one coherent message.
- [ ] **Running a search posts a spoken announcement** of the outcome ("12 results" / "1 result" / "No matches" / "ScreenCap isn't running"). ⚠️ Confirm the AppKit `NSAccessibility.post(.announcementRequested)` actually speaks on macOS 13 — if not, that's the known fallback risk.

## Keyboard (U3, U4)
- [ ] Opening Search (sidebar → Search) places the caret in the field; typing works without a click. Leaving and returning re-focuses.
- [ ] Field Return runs a search.
- [ ] Arrow keys move a visible selection through results. ⚠️ Verify the row `Button` does not swallow `List` arrow-key selection — if it does, switch rows to plain selectable rows (the documented fallback).
- [ ] With a result selected and the field unfocused, Return opens it in Review at its moment.
- [ ] No double-fire: a single Return never both searches and opens.
- [ ] Mouse single-click still opens a result.
- [ ] (Optional) Does macOS 13 announce the selected row's selected state during arrow nav, or is `.accessibilityAddTraits(.isSelected)` needed? (plan deferred question)

## Dynamic Type (U5)
- [ ] System Settings → Accessibility → Display → larger text: result rows show app/snippet, time, and stream without clipping or overlap, across stream types and long snippets/app names. If broken, harden minimally (`ViewThatFits` / wrap / `@ScaledMetric` thumbnail) — do not redesign.
