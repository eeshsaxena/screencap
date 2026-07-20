import Foundation

// SCR-174 U6 — pure conversion from a search result's absolute unix-ms anchor
// to a recording-relative seek position (seconds) for the Review window.
// Extracted so the null-origin guard and duration clamp are unit-testable
// without the SwiftUI/AVPlayer surface.
enum SearchSeek {
    /// Convert an absolute unix-ms anchor to recording-relative seconds.
    /// Guards a null/zero recording start (`startedAt <= 0` → seek to 0, per the
    /// nullable-timing rule) and clamps to a known duration.
    static func relativeSeconds(anchorMs: Int, startedAt: Double, durationSeconds: Double) -> Double {
        let relative = startedAt > 0 ? max(0, Double(anchorMs) / 1000 - startedAt) : 0
        return durationSeconds > 0 ? min(relative, durationSeconds) : relative
    }
}
