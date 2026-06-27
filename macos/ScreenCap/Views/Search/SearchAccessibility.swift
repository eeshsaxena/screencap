import Foundation

// SCR-183 — pure, unit-testable VoiceOver-label builders for the in-app Search
// surface. Deliberately free of SwiftUI so `ScreenCapTests` can assert the exact
// spoken strings (mirrors the `PrivacyBadgeStyle.derive` / `SnippetHighlighter`
// pattern). The views apply these via `.accessibilityLabel(...)`; the behavioral
// shell (focus, key events, announcement posting) stays in the view and is
// verified manually.
enum SearchAccessibility {

    /// Spoken label for a coverage chip — e.g. "On screen: 3 results",
    /// "Audio: no matches", "Activity: not indexed". Returns `nil` for `.notRun`
    /// (the chip renders no element, so there is nothing to announce). Unlike the
    /// compact visible text, the count is spelled out ("3 results") and the label
    /// never leans on the color dot, which carries the state visually only.
    static func coverageChipLabel(stream: String, state: StreamState) -> String? {
        let phrase: String
        switch state {
        case .notRun:
            return nil
        case .ok(let count):
            phrase = count == 1 ? "1 result" : "\(count) results"
        case .empty, .notIndexed, .degraded, .unavailable:
            // Shared with the visible chip text (SearchView.coverageText) so the
            // spoken and visible phrasings can't drift apart.
            phrase = coverageStatePhrase(for: state) ?? ""
        }
        return "\(stream): \(phrase)"
    }

    /// The count-independent coverage phrase, shared by the spoken chip label and
    /// the visible chip text so a future wording change updates both at once.
    /// `.ok`/`.notRun` stay each caller's concern — the visible chip shows a bare
    /// count while the spoken label spells "N results", and `.notRun` renders
    /// nothing visible and stays silent.
    static func coverageStatePhrase(for state: StreamState) -> String? {
        switch state {
        case .empty: return "no matches"
        case .notIndexed: return "not indexed"
        case .degraded: return "limited"
        case .unavailable: return "unavailable"
        case .ok, .notRun: return nil
        }
    }

    /// One combined label for a result row, replacing the fragmented `Text` runs
    /// VoiceOver would otherwise read as separate stops. Surfaces only what the
    /// row already shows — the snippet, never any underlying full OCR/transcript
    /// text — preserving SCR-174's pointer-only posture in the audio channel too.
    /// Leads with the most-recognizable token (app/snippet), matching the row's
    /// visual left-to-right order: "{primary}. {secondary}. {stream}. {time}[.
    /// Approximate, from audio]".
    static func resultRowLabel(_ item: SearchResultItem) -> String {
        var parts: [String] = []

        // Primary line: the snippet (screen/audio) or app name (activity).
        parts.append(item.primaryText)

        // Secondary line: window title for activity rows; the stream phrasing
        // ("On screen" / "Heard in audio") already conveys the stream for the
        // text streams, so only activity adds a distinct secondary.
        if item.stream == .activity, let title = item.title, !title.isEmpty {
            parts.append(title)
        }

        // Stream phrasing — but for activity the app name already led, so don't
        // repeat "Activity".
        switch item.stream {
        case .screen: parts.append("On screen")
        case .audio: parts.append("Heard in audio")
        case .activity: break
        }

        // Time, or an explicit spoken fallback for unanchored hits (the visible
        // row shows "—", which VoiceOver would read as "dash" or nothing).
        if item.anchorMs != nil {
            parts.append("at \(item.timeLabel)")
        } else {
            parts.append("time unknown")
        }

        var label = parts.joined(separator: ". ")
        if item.approximate {
            label += ". Approximate, from audio"
        }
        return label
    }

    /// Spoken announcement posted when a search completes, so a VoiceOver user
    /// who can't see the screen learns the outcome instead of hearing silence.
    /// Returns `nil` for phases that should not announce (`.idle`, `.searching`).
    static func searchOutcomeAnnouncement(for phase: SearchViewModel.Phase) -> String? {
        switch phase {
        case .idle, .searching:
            return nil
        case .daemonDown:
            return "ScreenCap isn\u{2019}t running"
        case .loaded(let results):
            let count = results.items.count
            if count == 0 {
                // Mirror the visible empty state: a time-scoped query over a
                // recorded-but-empty window reads "Nothing recorded then" rather
                // than the generic "No matches" (see SearchView.emptyRow).
                if results.timeWindow != nil, case .empty = results.coverage.activity {
                    return "Nothing recorded then"
                }
                return "No matches"
            }
            return count == 1 ? "1 result" : "\(count) results"
        }
    }

    /// SCR-178 U8 — spoken announcement posted when the backfill affordance
    /// reaches a **terminal** transition (done / paused / cancelled /
    /// start-failed), so a VoiceOver user learns the outcome of a long indexing
    /// run instead of hearing silence. Returns `nil` for every in-progress state
    /// (`hidden` / `offering` / `starting` / `indexing`): in particular the
    /// determinate `done/total` ticks do **not** announce, to avoid flooding
    /// VoiceOver during a run. Mirrors `searchOutcomeAnnouncement` (pure builder;
    /// the view posts the string on a terminal transition).
    static func backfillAnnouncement(for state: SearchViewModel.BackfillUIState) -> String? {
        switch state {
        case .hidden, .offering, .starting, .indexing:
            return nil
        case .done(let done, let total, let failed):
            if failed == 0 {
                return "Done \u{2014} your recording history is now searchable."
            }
            return "Indexed \(done) of \(total) recordings. \(failed) could not be indexed."
        case .paused(let done, let total):
            return "Indexed \(done) of \(total) so far \u{2014} resume to continue."
        case .cancelled:
            return "Indexing paused \u{2014} you can resume later."
        case .startFailed:
            return "Couldn\u{2019}t start indexing \u{2014} try again later."
        }
    }
}
