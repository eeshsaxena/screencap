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
    ///
    /// `isEncrypted` is the recording's frozen `cloud_e2ee` bit (SCR-253 U8): an
    /// encrypted stub adds the multi-device key guidance to the message, since a
    /// second Mac that hasn't received the key via iCloud Keychain can't read it.
    /// There is no key-presence API (KTD-5), so this is guidance, not detection —
    /// and never a corruption-looking error.
    static func decide(
        recording: String, anchorMs: Int?, isStub: Bool, isEncrypted: Bool = false
    ) -> InspectOpenDecision {
        guard !isStub else {
            if isEncrypted {
                return .unavailable(
                    message: "This recording (\(recording)) is end-to-end encrypted and was "
                        + "uploaded; the local copy was deleted. Reading it here needs the "
                        + "developer CLI to download it, and this Mac must hold the encryption "
                        + "key — sign in with the same account and turn on iCloud Keychain so "
                        + "the key syncs from the Mac that recorded it."
                )
            }
            return .unavailable(
                message: "This recording (\(recording)) was uploaded and the local copy was "
                    + "deleted. Retrieving it locally requires the developer CLI."
            )
        }
        return .open(recording: recording, seekMs: anchorMs)
    }
}
