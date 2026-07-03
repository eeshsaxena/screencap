import Foundation

// SCR-183 — pure, unit-testable VoiceOver-label builders for the search
// surface. Deliberately free of SwiftUI so `ScreenCapTests` can assert the
// exact spoken strings (mirrors the `SnippetHighlighter` pattern). The Recall
// palette (U10) applies these via `.accessibilityLabel(...)`; the behavioral
// shell (focus, key events, announcement posting) stays in the view and is
// verified manually. The retired sidebar Search pane's chip/announcement
// builders were deleted with it (U14) — only the result-row label survives.
enum SearchAccessibility {

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
}

extension SearchResultItem {
    /// Lead line: the on-screen/audio snippet, or the app for an activity row.
    var primaryText: String {
        switch stream {
        case .activity: return app ?? "Activity"
        case .screen, .audio: return (snippet?.isEmpty == false ? snippet! : "(no preview)")
        }
    }

    var timeLabel: String {
        guard let anchorMs else { return "—" }
        let f = DateFormatter()
        f.dateFormat = "HH:mm"
        return f.string(from: Date(timeIntervalSince1970: Double(anchorMs) / 1000))
    }
}
