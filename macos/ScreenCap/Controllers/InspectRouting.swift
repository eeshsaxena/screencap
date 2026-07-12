import Foundation

/// What a "look at this recording" tap should do, decided independently of
/// SwiftUI so it is unit-testable (mirrors `SearchSeek` / `TimelinePaneScrub`).
enum InspectOpenDecision: Equatable {
    /// Open the inspect window for `recording`, seeking to `seekMs` (absolute
    /// unix ms) when present; nil opens at the recording's start.
    case open(recording: String, seekMs: Int?)
    /// The recording has no local media to inspect (a stub: uploaded, local
    /// copy deleted) — surface this message instead of opening an empty window.
    case unavailable(message: String)
}

/// Shared routing for the two "looking" entry points (search results and the
/// Recordings list). Centralizing the decision keeps the stub message and the
/// open-vs-block branch identical at both callsites.
enum InspectRouting {
    /// A stub recording (uploaded; local media deleted) has nothing local to
    /// inspect, so it routes to the friendly download message both entry points
    /// share; everything else opens the inspect window, seeking when the tap
    /// carried a moment (search) and at the start when it did not (Recordings
    /// list, or an unanchored search hit).
    static func decide(recording: String, anchorMs: Int?, isStub: Bool) -> InspectOpenDecision {
        guard !isStub else {
            return .unavailable(
                message: "This recording (\(recording)) was uploaded and the local copy was "
                    + "deleted. Retrieving it locally requires the developer CLI."
            )
        }
        return .open(recording: recording, seekMs: anchorMs)
    }
}
