import Foundation

/// The `privacy` block emitted by `screencap settings --json` (SCR-17,
/// settings schema v2). Drives the SwiftUI first-run banner state machine
/// and the read-only mode header in `PrivacyPaneView`.
///
/// `hasPrivacySection` distinguishes "never written" from "present with
/// default values" — only the former triggers the first-launch fail-closed
/// `mode = internal` write. Reading the parsed `PrivacyConfig` instead would
/// conflate the two (defaults fill missing sections).
struct PrivacyStatus: Decodable, Equatable {
    let mode: String
    let setupSkipped: Bool
    let hasPrivacySection: Bool

    enum CodingKeys: String, CodingKey {
        case mode
        case setupSkipped = "setup_skipped"
        case hasPrivacySection = "has_privacy_section"
    }
}

/// Outer envelope for `screencap settings --json`. Only the `privacy` block
/// is consumed today; the other settings fields are decoded by other features
/// where applicable.
struct SettingsEnvelope: Decodable {
    struct Inner: Decodable {
        let privacy: PrivacyStatus?
        /// SCR-174: on-screen-text indexing flag (drives the search consent
        /// trigger). Optional/tolerant — older daemons omit it.
        let contentIndexEnabled: Bool?
        /// SCR-174: one-time consent decision, persisted as a settings bool so
        /// "declined" ≠ "off" and the prompt never re-fires (U7).
        let contentIndexConsentDeclined: Bool?
        /// SCR-174: recording chunk length (seconds) — used to estimate a
        /// transcript chunk's wall-clock position before snapping to a real
        /// timeline event.
        let chunkDuration: Double?

        enum CodingKeys: String, CodingKey {
            case privacy
            case contentIndexEnabled = "content_index_enabled"
            case contentIndexConsentDeclined = "content_index_consent_declined"
            case chunkDuration = "chunk_duration"
        }
    }

    let ok: Bool
    let settings: Inner

    enum CodingKeys: String, CodingKey {
        case ok
        case settings
    }
}
