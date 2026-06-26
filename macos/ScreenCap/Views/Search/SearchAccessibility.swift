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
        case .empty:
            phrase = "no matches"
        case .notIndexed:
            phrase = "not indexed"
        case .degraded:
            phrase = "limited"
        case .unavailable:
            phrase = "unavailable"
        }
        return "\(stream): \(phrase)"
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
            if count == 0 { return "No matches" }
            return count == 1 ? "1 result" : "\(count) results"
        }
    }
}
